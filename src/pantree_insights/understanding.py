"""Question understanding, the code half: turn the LLM's question frame into exact columns, filters and joins.

The understanding agent (agents.understand) says what the question is about (subjects), what it mentions
("Mumbai", "footwear", "order 12345") and which cuts it wants. This module checks every word against the data
and the concept map, and decides what can be decided without guessing:

- IDs: "order 12345" / "OD000012345" -> sales.order_id = 'OD000012345' (formats measured by profile.py)
- values: "Mumbai" exists in sales.ship_city, sellers.city, warehouses.city and product_master.brand_name; for
  a question about orders those are four different meanings, so unless the LLM (or an earlier answer) chose
  one, it becomes an open point for the user. Meanings in the same group (sub-category vs article type) are
  not worth a question: the first listed is used.
- cuts: a breakdown that does not exist for the subject (ad spend by city) becomes an open point offering
  the cuts that do exist in the same group (region).
- the time column follows the subject and the meaning ("delivered in October" -> sales.delivered_at)
- the join path is built in code from the subject's table (joins.py)

The result is rendered as a "Question understanding" section that the planner and SQL writer must follow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import get_close_matches

from .joins import JoinGraph
from .profile import IdFormat
from .semantic import ConceptEntry, Concepts


# ----------------------------------------------------------------------------- IDs
@dataclass
class IdMatch:
    text: str                       # as written: "order 12345"
    column: str                     # sales.order_id
    value: str                      # OD000012345
    exists: bool


class IdResolver:
    def __init__(self, formats: dict[str, IdFormat], entities: dict[str, str], con=None):
        self.con = con
        self.entities = {word: column for word, column in entities.items() if column in formats}
        self.formats = {column: formats[column] for column in self.entities.values()}

    def _exists(self, column: str, value: str) -> bool:
        if self.con is None:
            return True
        table, col = column.split(".")
        cursor = self.con.cursor()
        try:
            return cursor.execute(f'SELECT 1 FROM {table} WHERE "{col}" = ? LIMIT 1', [value]).fetchone() is not None
        finally:
            cursor.close()

    def find(self, question: str) -> list[IdMatch]:
        found: dict[tuple[str, str], IdMatch] = {}
        # 1. full identifiers typed as stored: OD000012345, CMP00012, P000123-M
        for column, fmt in self.formats.items():
            for m in re.finditer(rf"(?<![A-Za-z0-9]){fmt.regex}(?![A-Za-z0-9])", question, re.I):
                value = m.group(0).upper()
                found.setdefault((column, value), IdMatch(m.group(0), column, value, self._exists(column, value)))
        # 2. "order 12345", "customer #4521", "ticket id: 77", "campaign CMP12"
        words = "|".join(sorted(map(re.escape, self.entities), key=len, reverse=True))
        pattern = rf"\b({words})s?\s*(?:id|no\.?|number)?\s*[:#]?\s*([A-Za-z]{{0,5}}\d{{1,12}})\b"
        for m in re.finditer(pattern, question, re.I):
            column = self.entities[m.group(1).lower()]
            fmt = self.formats[column]
            raw = m.group(2).upper()
            prefix, digits = re.match(r"([A-Z]*)(\d+)", raw).groups()
            if (prefix and prefix != fmt.prefix) or len(digits) > fmt.digits or fmt.suffix:
                continue                                  # not this entity's format (or a sku needing a size)
            value = fmt.prefix + digits.zfill(fmt.digits)
            found.setdefault((column, value), IdMatch(m.group(0), column, value, self._exists(column, value)))
        return list(found.values())


# ----------------------------------------------------------------------------- resolution
@dataclass
class Filter:
    text: str                       # as the user wrote it
    concept: str
    column: str
    value: str
    expression: str | None = None   # derived cut (COD vs Prepaid): filter on the expression, not the column

    def sql(self) -> str:
        return f"{self.expression or self.column} = '{self.value}'"


@dataclass
class SubjectView:
    name: str
    table: str
    where: str | None
    time_column: str
    time_label: str
    filters: list[Filter] = field(default_factory=list)
    breakdowns: list[tuple[str, str]] = field(default_factory=list)      # (concept, column)
    id_filters: list[IdMatch] = field(default_factory=list)
    joins: str = ""
    event_required: bool = False
    expressions: dict[str, str] = field(default_factory=dict)          # concept -> derived breakdown expression
    own_joins: dict[str, str] = field(default_factory=dict)            # table -> join a concept defines itself

    def breakdown_sql(self) -> dict[str, str]:
        """Result column name -> SQL for each breakdown (derived cuts are named after their concept)."""
        return {(concept if concept in self.expressions else column.split(".")[1]):
                self.expressions.get(concept, column) for concept, column in self.breakdowns}


@dataclass
class OpenPoint:
    """A question for the user. `meaning` maps each option to what code should do with it (internal)."""
    key: str                        # "mention:Mumbai", "breakdown:city", "subject"
    question: str
    options: list[str]
    default: str
    why: str
    meaning: dict[str, str] = field(default_factory=dict)
    required: bool = False            # unresolved values cannot be guessed even when defaults were requested

    def public(self) -> dict:
        return {"question": self.question, "options": self.options, "default": self.default, "why": self.why}


@dataclass
class Resolution:
    subjects: list[SubjectView]
    open_points: list[OpenPoint]
    notes: list[str]

    def tables(self) -> list[str]:
        out: list[str] = []
        for s in self.subjects:
            out += [s.table] + [c.split(".")[0] for _, c in s.breakdowns] + [f.column.split(".")[0] for f in s.filters]
            out += [i.column.split(".")[0] for i in s.id_filters]
        return list(dict.fromkeys(out))

    def to_prompt(self, period: str | None = None, time_grain: str | None = None) -> str:
        if not self.subjects:
            return ""
        lines = ["Resolved in code from the data and the concept map. Follow it exactly."]
        if period:
            lines.append(f"Period: {period}")
            if period.strip().lower() == "all data":
                lines.append("All data means no date bounds; do not substitute the manifest's first/last day.")
        for s in self.subjects:
            where = f"; always filter {s.where}" if s.where else ""
            lines.append(f"\nSubject: {s.name} (table {s.table}{where})")
            lines.append(f"Time column: {s.time_column} ({s.time_label})")
            if time_grain:
                lines.append(f"Time grain: one row per {time_grain}: date_trunc('{time_grain}', {s.time_column}) "
                             f"AS {time_grain}, ordered by {time_grain}")
            if s.event_required:
                lines.append(f"Required event filter: {s.time_column} IS NOT NULL (also for all data)")
            for f in s.filters:
                lines.append(f"Filter: {f.sql()}  (\"{f.text}\" = {f.concept.replace('_', ' ')})")
            for i in s.id_filters:
                lines.append(f"Filter: {i.column.split('.')[1]} = '{i.value}'  (\"{i.text}\")")
            for concept, column in s.breakdowns:
                if concept in s.expressions:
                    lines.append(f"Breakdown: {concept.replace('_', ' ')} -> {s.expressions[concept]} AS {concept} "
                                 "(one row per value of this expression, not per stored value)")
                else:
                    lines.append(f"Breakdown: {concept.replace('_', ' ')} -> {column}")
            lines.append("Join path (built from the approved joins; use these joins):\n" + s.joins)
        if self.notes:
            lines.append("\nNotes:\n" + "\n".join(f"- {n}" for n in self.notes))
        return "\n".join(lines)


def _numeric(graph: JoinGraph, column: str) -> bool:
    table, _, name = column.partition(".")
    kind = graph.schema.tables.get(table).columns.get(name, "") if table in graph.schema.tables else ""
    return kind.split(" - ")[0].strip() in ("number", "integer")


def _plain(text: str) -> str:
    """'Sale-event days' -> 'sale event day': case, hyphens and plurals do not change a label."""
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
                    for w in re.sub(r"[-_]", " ", text.casefold()).split())


def _term(text: str, terms: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    """The glossary term a mention is ("active products" -> active product): exact, or a plural, or a term
    that makes up most of the words."""
    text = re.sub(r"\s+", " ", text.strip().strip("'\"").casefold())
    for candidate in (text, text[:-1] if text.endswith("s") else text, text[:-2] if text.endswith("es") else text):
        if candidate in terms:
            return terms[candidate]
    found = [t for t in terms if re.search(rf"(?<![\w-]){re.escape(t)}s?(?![\w-])", text)]
    best = max(found, key=len, default=None)
    return terms[best] if best and len(best) >= 0.6 * len(text) else None


def _find_value(text: str, value_columns: dict[str, list[str]], aliases: dict[str, str]) -> str | None:
    """Exact spelling wins; a case-insensitive match is safe only when it is unique."""
    text = text.strip().strip("'\"")
    if text in value_columns:
        return text
    matches = [v for v in value_columns if v.casefold() == text.casefold()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None
    alias = aliases.get(text.casefold())
    if alias in value_columns:
        return alias
    plain = [v for v in value_columns if _plain(v) == _plain(text)]   # 'organic search' -> organic_search
    return plain[0] if len(plain) == 1 else None


def resolve(frame, concepts: Concepts, graph: JoinGraph, value_columns: dict[str, list[str]],
            ids: list[IdMatch], choices: dict[str, str] | None = None,
            aliases: dict[str, str] | None = None, terms: dict[str, str] | None = None,
            answered: list[str] | None = None) -> Resolution:
    """`frame`: agents.QuestionFrame. `choices`: earlier answers, key -> chosen meaning (concept or subject).
    `aliases`: lowercased alias -> stored value (glossary value_aliases)."""
    choices, aliases = choices or {}, aliases or {}
    terms = {t.casefold(): (t, meaning) for t, meaning in (terms or {}).items()}
    answered_words = {a.strip().casefold() for a in answered or [] if a}
    open_points: list[OpenPoint] = []
    notes: list[str] = []

    names = [s for s in frame.subjects if s in concepts.subjects]
    if "subject" in choices and choices["subject"] in concepts.subjects:
        names = [choices["subject"]]
    if not names:
        options = list(concepts.subjects)[:4]
        open_points.append(OpenPoint("subject", "What should I measure for this question?",
                                     [concepts.subjects[o].about.split(",")[0] for o in options],
                                     concepts.subjects[options[0]].about.split(",")[0],
                                     "the answer decides which data is used",
                                     {concepts.subjects[o].about.split(",")[0]: o for o in options}))
        return Resolution([], open_points, notes)

    views = []
    for name in names:
        subj = concepts.subjects[name]
        time_column = subj.time_meanings.get(frame.time_meaning or "", subj.time)
        label = next((k for k, v in subj.time_meanings.items() if v == time_column), "date") if time_column \
            else "date"
        view = SubjectView(name, subj.table, subj.where, time_column, f"{name} by the date they were {label}"
                           if subj.time_meanings else f"{name} by {time_column.split('.')[1]}",
                           event_required=time_column != subj.time)

        derived = {c: e[name] for c, e in concepts.concepts.items() if name in e and e[name].expression}
        breakdowns = list(frame.breakdowns)
        def is_member(mention) -> bool:                  # a group being compared, never a filter
            return mention.role == "comparison" or bool(
                frame.comparison and mention.concept in frame.breakdowns
                and sum(m.concept == mention.concept for m in frame.mentions) > 1)

        members = [m.text for m in frame.mentions if is_member(m)]
        absorbed: set[str] = set()                      # member concepts a derived cut replaced
        for concept, entry in derived.items():          # "COD versus prepaid": the comparison IS the derived cut
            labels = {_plain(lab): lab for lab in entry.labels()}
            if members and sum(_plain(t) in labels for t in members) >= 2 and concept not in breakdowns:
                member_concepts = {m.concept for m in frame.mentions if is_member(m)}
                stored = [c for c in breakdowns if c in member_concepts
                          or (concepts.column(c, name) or ConceptEntry(column="")).column == entry.column]
                breakdowns = [c for c in breakdowns if c not in stored] + [concept]
                absorbed |= member_concepts
                notes.append(f"Comparison {' vs '.join(members)} is the {concepts.label(concept)} cut: "
                             f"{entry.expression}")

        for mention in frame.mentions:
            if mention.concept in ("id", "metric", "period"):    # "other" = the LLM was unsure: resolve it here
                continue
            label = next(((c, e, lab) for c, e in derived.items() for lab in e.labels()
                          if _plain(lab) == _plain(mention.text)), None)
            if label and not is_member(mention) and not (label[2] in value_columns
                                                              and label[1].column in value_columns[label[2]]):
                concept, entry, lab = label                 # "prepaid orders": a value of a derived cut
                view.filters.append(Filter(mention.text, concept, entry.column, lab, entry.expression))
                continue
            if is_member(mention):
                notes.append(f"Comparison group: {mention.text} ({mention.concept}); keep the other comparison groups.")
                member_entry = concepts.column(mention.concept, name)
                if member_entry and member_entry.note:
                    note = f"{concepts.label(mention.concept)} for {name}: {member_entry.note}"
                    if note not in notes:
                        notes.append(note)
                continue
            value = _find_value(mention.text, value_columns, aliases)
            entry_now = concepts.column(mention.concept, name)
            numeric = entry_now and not entry_now.expression and _numeric(graph, entry_now.column)
            if value is None and (numeric or (re.search(r"\d", mention.text) and mention.concept == "other")):
                notes.append(f"'{mention.text}' is a numeric condition"
                             + (f" on {entry_now.column}" if numeric else "") + ": apply it as stated")
                continue                                    # "top 5 positions": a range, not a stored value
            term = _term(mention.text, terms)
            if value is None and term:                      # business vocabulary, not a stored value
                notes.append(f"'{term[0]}' is a business term: {term[1]}")
                continue
            said = mention.text.strip().casefold()
            if value is None and said and any(re.search(rf"(?<![\w-]){re.escape(said)}(?![\w-])", a)
                                              for a in answered_words):
                notes.append(f"'{mention.text}' is the user's answer to a clarifying question: apply it as chosen")
                continue                                    # a settled choice, not a value to look up
            value_key = f"value:{name}:{mention.concept}:{mention.text}"
            entry = concepts.column(mention.concept, name)
            literal_choice = f"Use exactly '{mention.text}' (may return no rows)"
            choice = choices.get(value_key)
            if value is None:
                eligible = {v: cols for v, cols in value_columns.items()
                            if (entry and entry.column in cols) or (entry is None and any(
                                name in entries and entries[name].column in cols
                                for entries in concepts.concepts.values()))}
                options = [v for v in eligible if v.casefold() == mention.text.casefold()]
                if not options:
                    options = get_close_matches(mention.text, list(eligible), n=3, cutoff=0.6)
                meanings = {v: v for v in options}
                if entry:
                    meanings[literal_choice] = mention.text
                if choice in meanings.values() and choice != "__revise__":
                    value = choice
                    if choice == mention.text and entry:
                        view.filters.append(Filter(mention.text, mention.concept, entry.column, choice))
                        notes.append(f"No indexed match for '{choice}'; using the user's exact spelling.")
                        continue
                else:
                    if not meanings:
                        meanings = {"Revise the question with an exact value": "__revise__"}
                    open_points.append(OpenPoint(
                        value_key, f"Which exact value should '{mention.text}' mean for {name}?",
                        list(meanings), next(iter(meanings)),
                        "the value is missing or has multiple case-distinct matches; it must not be dropped",
                        meanings, required=True))
                    continue
            columns = set(value_columns.get(value, []))
            candidates = [c for c, entries in concepts.concepts.items()
                          if name in entries and entries[name].column in columns]
            key = f"mention:{value}"
            pick = choices.get(key) if choices.get(key) in candidates else None
            pick = pick or (mention.concept if mention.concept in candidates else None)
            if pick is None and len({concepts.group(c) for c in candidates}) == 1:
                pick = candidates[0]
            if pick is None and candidates:
                options = {concepts.label(c): c for c in candidates}
                open_points.append(OpenPoint(key, f"What does '{mention.text}' refer to in this question?",
                                             list(options), next(iter(options)),
                                             f"'{value}' exists as more than one thing in the data", options))
                continue
            if pick is None:
                notes.append(f"'{value}' exists in the data ({', '.join(sorted(columns))}) but not as a cut of {name}")
                continue
            view.filters.append(Filter(mention.text, pick, concepts.column(pick, name).column, value))
            if concepts.column(pick, name).note:
                notes.append(f"{concepts.label(pick)} for {name}: {concepts.column(pick, name).note}")

        for concept in breakdowns:
            entry = concepts.column(concept, name)
            if entry:
                view.breakdowns.append((concept, entry.column))
                if entry.expression:
                    view.expressions[concept] = entry.expression
                if entry.note:
                    notes.append(f"{concepts.label(concept)} for {name}: {entry.note}")
                continue
            key = f"breakdown:{concept}"
            same_group = [c for c, e in concepts.concepts.items()
                          if name in e and concepts.group(c) == concepts.group(concept) and c != concept]
            if choices.get(key) in same_group:
                view.breakdowns.append((choices[key], concepts.column(choices[key], name).column))
            elif choices.get(key) == "skip":
                continue
            elif concept in concepts.concepts:
                options = {concepts.label(c): c for c in same_group} | {"Leave this breakdown out": "skip"}
                open_points.append(OpenPoint(
                    key, f"{subj.about.split(',')[0].capitalize()} cannot be split by {concepts.label(concept)}. "
                    "What should I use instead?", list(options), next(iter(options)),
                    f"the data has no {concepts.label(concept)} for {name}", options))
            else:                                           # never drop a requested split silently
                notes.append(f"'{concept.replace('_', ' ')}' is not a known cut of {name}; group by the column that "
                             "the question means, from the tables in the context, and name the result column after it")

        for match in ids:
            id_col = match.column.split(".")[1]
            own = f"{subj.table}.{id_col}"
            column = own if id_col in graph.schema.tables[subj.table].columns else match.column
            view.id_filters.append(IdMatch(match.text, column, match.value, match.exists))
            if not match.exists:
                notes.append(f"{match.text} ({match.value}) does not exist in the data; the result will be empty")

        tables = [c.split(".")[0] for _, c in view.breakdowns] + [f.column.split(".")[0] for f in view.filters] + \
            [i.column.split(".")[0] for i in view.id_filters]
        used = [c for c, _ in view.breakdowns] + [f.concept for f in view.filters] + \
            [m.concept for m in frame.mentions if is_member(m) and m.concept not in absorbed]
        for concept in dict.fromkeys(used):           # e.g. an event measured by its date window, not event_id
            entry = concepts.column(concept, name)
            if entry and entry.join:
                view.own_joins[entry.join.split()[0]] = entry.join
        keyed = [t for t in dict.fromkeys(tables) if t != subj.table and t not in view.own_joins]
        view.joins = graph.render(subj.table, keyed)
        if view.own_joins:
            first, _, rest = view.joins.partition("\n")
            view.joins = "\n".join([first, *(f"JOIN {j}" for j in view.own_joins.values())] + ([rest] if rest else []))
            notes.append("Use the join path exactly as given: " + "; ".join(
                f"{t} is joined by {j.split(' ON ', 1)[1]}, not by its ID" for t, j in view.own_joins.items()))
        views.append(view)
    return Resolution(views, open_points, notes)


# ----------------------------------------------------------------------------- read-back for confirmation
def readback(frame, res: Resolution, concepts: Concepts, metrics) -> str:
    """What will be computed, in business words, built from the resolved understanding (not from the model's own
    summary), so what the user confirms is exactly what the SQL must follow."""
    lines = [f"**Question:** {frame.restated_question}"]
    measures = []
    for m in frame.measures or ["(not stated)"]:
        if m in metrics.metrics:
            measures.append(f"{metrics.metrics[m].label}: {metrics.metrics[m].description}")
        elif m in metrics.custom_metrics:
            measures.append(f"{metrics.custom_metrics[m].label}: {metrics.custom_metrics[m].description}")
        else:
            measures.append(m)
    lines.append("**Measure:** " + "; ".join(measures))
    for v in res.subjects:
        about = concepts.subjects[v.name].about.split(",")[0]
        cuts = [concepts.label(c) for c, _ in v.breakdowns] + ([frame.time_grain] if frame.time_grain else [])
        lines.append(f"**About:** {about}" + (f", split by {', '.join(cuts)}" if cuts else ""))
        lines.append(f"**Period:** {frame.period or 'all data'}, {v.time_label}")
        filters = [f"{concepts.label(f.concept)} = {f.value}" for f in v.filters] + \
                  [f"{i.column.split('.')[1]} = {i.value}" for i in v.id_filters]
        lines.append("**Filters:** " + ("; ".join(filters) if filters else "none"))
    if frame.comparison:
        lines.append(f"**Compared with:** {frame.comparison}")
    if res.notes:
        lines.append("**Note:** " + " ".join(res.notes))
    return "\n".join(lines)
