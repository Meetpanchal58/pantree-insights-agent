"""Deterministic safety checks (no LLM): questions in, SQL before it runs, numbers in the final answer.

The database connection is locked down as well (see db.connect); these checks catch problems earlier and
explain them in words the SQL writer can fix.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .config import get_settings
from .semantic import Schema, load_schema

# Statement / clause types that change data, schema, settings or files.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.TruncateTable,
    exp.Command, exp.Set, exp.Pragma, exp.Copy, exp.Attach, exp.Detach, exp.Use, exp.Transaction,
    exp.Commit, exp.Rollback, exp.Into, exp.LoadData, exp.Grant,
)
# Functions that read files / the environment or run arbitrary queries.
FORBIDDEN_FUNCTIONS = {
    "read_csv", "read_csv_auto", "read_parquet", "parquet_scan", "read_json", "read_json_auto",
    "read_ndjson", "read_text", "read_blob", "glob", "sniff_csv", "getenv", "current_setting",
    "query", "query_table", "iceberg_scan", "delta_scan", "sqlite_scan", "postgres_scan", "mysql_scan",
    "duckdb_settings", "duckdb_extensions", "duckdb_secrets", "pragma_version", "load_extension",
}
AGGREGATES: tuple[type[exp.Expression], ...] = (exp.AggFunc,)


@dataclass
class SQLCheck:
    ok: bool
    sql: str                                   # normalised SQL (pretty-printed) when ok
    errors: list[str] = field(default_factory=list)
    tables: set[str] = field(default_factory=set)


def check_sql(sql: str, schema: Schema | None = None, max_rows: int | None = None) -> SQLCheck:
    """Allow exactly one read-only, analytical SELECT over the allowed tables."""
    schema = schema or load_schema()
    max_rows = max_rows or get_settings().max_rows
    allowed_tables = set(schema.tables)
    blocked_columns = schema.blocked.column_names()
    errors: list[str] = []

    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as error:
        return SQLCheck(False, sql, [f"SQL could not be parsed: {str(error).splitlines()[0]}"])
    if len(statements) != 1:
        return SQLCheck(False, sql, [f"Exactly one statement is allowed, found {len(statements)}."])
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        return SQLCheck(False, sql, [f"Only SELECT queries are allowed, not {tree.key.upper()}."])
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            errors.append(f"{node.key.upper()} is not allowed; queries must be read-only.")

    # Tables: only allowed ones (or names defined in a WITH clause), never qualified or table functions.
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    used: set[str] = set()
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            errors.append(f"Table functions are not allowed ({table.sql(dialect='duckdb')[:60]}).")
            continue
        name = table.name.lower()
        if table.args.get("db") or table.args.get("catalog"):
            errors.append(f"Use plain table names, not {table.sql(dialect='duckdb')}.")
        elif name in cte_names:
            continue
        elif name in schema.blocked.tables:
            errors.append(f"Table {name} is not available: {schema.blocked.tables[name]}.")
        elif name not in allowed_tables:
            errors.append(f"Unknown table {name}. Available tables: {', '.join(sorted(allowed_tables))}.")
        else:
            used.add(name)

    for func in tree.find_all(exp.Func):
        name = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
        if name in FORBIDDEN_FUNCTIONS:
            errors.append(f"Function {name}() is not allowed.")

    for column in tree.find_all(exp.Column):
        if column.name.lower() in blocked_columns:
            errors.append(f"Column {column.name} is not available (personal or internal data).")

    # No SELECT *: name the columns you need (COUNT(*) is fine).
    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            if isinstance(projection, exp.Star) or (
                    isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)):
                errors.append("SELECT * is not allowed; select the columns you need, or aggregate.")

    # Analytical, not a raw dump: the final SELECT aggregates (or only reads aggregated CTEs /
    # subqueries), or returns a limited number of rows.
    ctes = {cte.alias_or_name.lower(): cte.this for cte in tree.find_all(exp.CTE)}
    aggregated = _aggregated(tree, ctes)
    limit = tree.args.get("limit")
    limit_value = _int(limit.expression) if limit is not None else None
    if limit is not None and (limit_value is None or limit_value > max_rows):
        errors.append(f"LIMIT must be a number no larger than {max_rows}.")
    if not aggregated and limit is None:
        errors.append(f"Return summarised results: aggregate (GROUP BY / SUM / COUNT ...) "
                      f"or add LIMIT (at most {max_rows}) for a list of top rows.")

    errors += whole_row_errors(tree, schema, cte_names)
    if not errors:
        qualified, problem = _qualify(tree, schema)
        if problem:
            errors.append(problem)
        else:
            errors += join_errors(qualified, schema, cte_names)
            errors += identifier_grain_errors(qualified, schema, max_rows)

    if errors:
        return SQLCheck(False, sql, list(dict.fromkeys(errors)), used)
    return SQLCheck(True, tree.sql(dialect="duckdb", pretty=True), [], used)


def _approved_joins(schema: Schema) -> set[frozenset]:
    pairs = set()
    for join in schema.joins:
        left, right = (side.strip().split(".") for side in join.split("="))
        pairs.add(frozenset({tuple(left), tuple(right)}))
    return pairs


def _qualify(tree: exp.Expression, schema: Schema) -> tuple[exp.Expression | None, str | None]:
    """Resolve every column to its table (so unqualified columns cannot hide an unapproved join)."""
    from sqlglot.optimizer.qualify import qualify

    try:
        return qualify(tree.copy(), schema=schema.column_types(), dialect="duckdb", validate_qualify_columns=True,
                       quote_identifiers=False, expand_stars=False), None
    except Exception as error:                     # unknown or ambiguous column
        return None, (f"Could not resolve the columns ({str(error).splitlines()[0][:160]}). Qualify columns "
                      "with their table alias and use only columns listed in the context.")


def join_errors(tree: exp.Expression, schema: Schema, cte_names: set[str]) -> list[str]:
    """Equality predicates between two different base tables (in JOIN ... ON, USING or WHERE, including
    comma joins) must be approved joins from schema.yaml. Joins with CTEs/subqueries and range joins are
    allowed. Expects a qualified tree (every column has its table alias)."""
    approved = _approved_joins(schema)
    alias: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if name in schema.tables and name not in cte_names:
            alias[table.alias_or_name.lower()] = name
    errors = []
    for eq in tree.find_all(exp.EQ):
        a, b = eq.left, eq.right
        if not (isinstance(a, exp.Column) and isinstance(b, exp.Column) and a.table and b.table):
            continue
        ta, tb = alias.get(a.table.lower()), alias.get(b.table.lower())
        if not ta or not tb or ta == tb:
            continue
        if frozenset({(ta, a.name.lower()), (tb, b.name.lower())}) not in approved:
            errors.append(f"Join {ta}.{a.name} = {tb}.{b.name} is not an approved join path "
                          "(see 'How these tables join'); it can duplicate or mismatch rows.")
    for join in tree.find_all(exp.Join):
        right = join.this
        right_table = alias.get(right.alias_or_name.lower()) if isinstance(right, exp.Table) else None
        for ident in join.args.get("using") or []:
            column = ident.name.lower()
            if right_table and not any(frozenset({(other, column), (right_table, column)}) in approved
                                       for other in set(alias.values()) if other != right_table):
                errors.append(f"JOIN {right_table} USING ({column}) is not an approved join path.")
    return errors


def whole_row_errors(tree: exp.Expression, schema: Schema, cte_names: set[str]) -> list[str]:
    """Whole rows passed to functions (to_json(a), struct_pack(a.*), row(a.*)) would leak every column."""
    aliases = {t.alias_or_name.lower() for t in tree.find_all(exp.Table)} | cte_names
    columns = {c for info in schema.tables.values() for c in info.columns}
    errors = []
    for func in tree.find_all(exp.Func):
        if isinstance(func, exp.Count):
            continue
        for arg in func.iter_expressions():
            star = isinstance(arg, exp.Star) or (isinstance(arg, exp.Column) and isinstance(arg.this, exp.Star))
            whole = isinstance(arg, exp.Column) and not arg.table and arg.name.lower() in aliases \
                and arg.name not in columns
            if star or whole:
                errors.append("Whole-row expressions (a table or alias.* inside a function) are not allowed; "
                              "select the specific columns you need.")
    return errors


def identifier_grain_errors(tree: exp.Expression, schema: Schema, max_rows: int) -> list[str]:
    """A final result grouped by a row-level identifier (customer_id, order_id, ...) must be a ranked
    top-N; grouping by identifiers inside CTEs/subqueries that are aggregated again is fine."""
    identifiers = {c.lower() for c in schema.identifier_columns}
    outer = tree if isinstance(tree, exp.Select) else None
    if outer is None or not outer.args.get("group"):
        return []
    grouped = {c.name.lower() for e in outer.args["group"].expressions for c in e.find_all(exp.Column)}
    # GROUP BY 1, 2: resolve positions to the projected columns
    for e in outer.args["group"].expressions:
        if isinstance(e, exp.Literal) and e.is_int and 0 < int(e.name) <= len(outer.expressions):
            grouped |= {c.name.lower() for c in outer.expressions[int(e.name) - 1].find_all(exp.Column)}
    hit = sorted(grouped & identifiers)
    if not hit:
        return []
    limit = outer.args.get("limit")
    limit_value = _int(limit.expression) if limit is not None else None
    if outer.args.get("order") and limit_value is not None and limit_value <= max_rows:
        return []
    return [f"Grouping the result by {', '.join(hit)} returns one row per {hit[0].replace('_id', '')}; "
            f"make it a ranked top-N (ORDER BY the metric DESC LIMIT <= {max_rows}) or aggregate across them."]


def _aggregated(query: exp.Expression, ctes: dict[str, exp.Expression], depth: int = 0) -> bool:
    """True if the query's result is summarised: it aggregates, or every source it reads is summarised."""
    if depth > 10:
        return False
    if isinstance(query, exp.Subquery):
        return _aggregated(query.this, ctes, depth + 1)
    if isinstance(query, exp.SetOperation):          # UNION / INTERSECT / EXCEPT
        return _aggregated(query.left, ctes, depth + 1) and _aggregated(query.right, ctes, depth + 1)
    if not isinstance(query, exp.Select):
        return False
    if query.args.get("group") or any(p.find(*AGGREGATES) and not p.find(exp.Window)
                                      for p in query.expressions):
        return True
    sources = []
    from_clause = query.args.get("from_") or query.args.get("from")    # key name differs by sqlglot version
    if from_clause:
        sources.append(from_clause.this)
    sources += [join.this for join in query.args.get("joins") or []]
    if not sources:
        return True                                  # SELECT 1, constants only
    for source in sources:
        if isinstance(source, exp.Table) and source.name.lower() in ctes:
            if not _aggregated(ctes[source.name.lower()], ctes, depth + 1):
                return False
        elif isinstance(source, exp.Subquery):
            if not _aggregated(source.this, ctes, depth + 1):
                return False
        else:
            return False                             # a base table read row by row
    return True


def _int(node: exp.Expression | None) -> int | None:
    try:
        return int(node.name) if isinstance(node, exp.Literal) and node.is_int else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- questions
MAX_QUESTION_CHARS = 500
_PEOPLE = r"(customers?|users?|buyers?|shoppers?|clients?|persons?|people|members?)"
_CONTACT = r"(e-?mails?|e-?mail address(es)?|phone( numbers?)?|mobile( numbers?)?|contact( details| info(rmation)?)?|" \
           r"(home |street |postal |full |exact )?address(es)?|names?)"
PERSONAL_DATA = re.compile(
    # "email/phone/address/name of/for (the/each/top ...) customer(s)"
    rf"\b{_CONTACT} (of|for) (\w+ ){{0,3}}?{_PEOPLE}\b|"
    # "customer emails", "customers' phone numbers", "buyer contact details"
    rf"\b{_PEOPLE}'?s?'? (e-?mails?|e-?mail address(es)?|phone numbers?|mobile numbers?|"
    r"contact (details|info(rmation)?)|home address(es)?|addresses|full names?|first names?|last names?)\b|"
    # explicit personal-data phrases on their own
    r"\b(contact details|home address(es)?|street address(es)?|full address(es)?|address line|"
    r"first names?|last names?|full names?|pii|personal data|personal details)\b", re.I)
# Aggregate questions that merely mention mobile, phone orders, email campaigns, delivery address region or
# password-reset tickets are allowed; the SQL guardrails and the locked database are the real enforcement.
INJECTION = re.compile(
    r"((ignore|disregard|forget|override|bypass) (all |any |the |your )?(previous|prior|above|earlier|"
    r"preceding) (instructions|rules|prompts?)|system prompt|you are now|"
    r"developer mode|jailbreak|drop\s+table|delete\s+from|;\s*--)", re.I)


@dataclass
class QuestionCheck:
    ok: bool
    reason: str | None = None


def check_question(question: str) -> QuestionCheck:
    """Cheap checks before any LLM call. Topic relevance is judged later by the planner."""
    text = question.strip()
    if not text:
        return QuestionCheck(False, "The question is empty.")
    if len(text) > MAX_QUESTION_CHARS:
        return QuestionCheck(False, f"Please keep questions under {MAX_QUESTION_CHARS} characters.")
    if INJECTION.search(text):
        return QuestionCheck(False, "That looks like an instruction to the system rather than a business question.")
    if PERSONAL_DATA.search(text):
        return QuestionCheck(False, "Personal customer data (names, emails, phone numbers, addresses) is not "
                                    "available. Ask about customers in aggregate instead, e.g. by city or segment.")
    return QuestionCheck(True)
