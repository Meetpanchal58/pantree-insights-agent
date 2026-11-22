"""Interpret: readings."""
from __future__ import annotations

import json
import re
from datetime import date
from difflib import SequenceMatcher

from ..agents import ClarifyingQuestion
from ..spec import Catalog, QuerySpec
from .readback import readback


def _plain(text: str) -> str:
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
                    for w in re.sub(r"[-_]", " ", str(text).casefold()).split())


def normalise(spec: QuerySpec, cat: Catalog, aliases: dict[str, str]) -> QuerySpec:
    """Map filter values to the stored spelling when that is unambiguous ('organic search' -> organic_search,
    'cash on delivery' -> COD). Anything not matched is left for validation to report."""
    def fix(filters):
        out = []
        for f in filters:
            try:
                expression, _, entry = cat.resolve(f.field, spec.table)
            except Exception:                          # noqa: BLE001 - validation reports it
                out.append(f)
                continue
            allowed = entry.labels() if entry is not None and entry.expression else cat.values_of(expression)
            if not allowed or f.op not in ("eq", "in", "not_in"):
                out.append(f)
                continue
            values = []
            for v in f.values:
                if isinstance(v, bool) or str(v) in allowed:
                    values.append(v)
                    continue
                alias = aliases.get(str(v).casefold())
                matches = ([a for a in allowed if a == alias]
                           or [a for a in allowed if a.casefold() == str(v).casefold()]
                           or [a for a in allowed if _plain(a) == _plain(v)])
                values.append(matches[0] if len(matches) == 1 else v)
            out.append(f.model_copy(update={"values": values}))
        return out
    measures = [m.model_copy(update={"where": fix(m.where)}) for m in spec.measures]
    subject = cat.subject_of.get(spec.table)
    always = cat.concepts.subjects[subject].where if subject else None

    def redundant(f) -> bool:                          # the table's own rule restated (line_type = 'Sale')
        if not always or f.op not in ("eq", "in") or len(f.values) != 1:
            return False
        try:
            expression = cat.resolve(f.field, spec.table)[0]
        except Exception:                              # noqa: BLE001 - validation reports it
            return False
        return f"{expression} = '{f.values[0]}'" == always
    filters = [f for f in fix(spec.filters) if not redundant(f)]
    dimensions, seen, reserved = [], {}, set()
    dimension_aliases = {}
    for d in spec.dimensions:
        try:
            expression, name, _ = cat.resolve(d, spec.table)
            if expression in seen:
                dimension_aliases[d] = seen[expression]
                continue
            seen[expression] = d
            reserved.add(name)
            if expression in cat.labels:
                reserved.add(cat.labels[expression])
        except ValueError:
            pass
        dimensions.append(d)
    if spec.time and spec.time.grain:
        reserved.add(spec.time.grain)
    renamed = {}
    if len({m.name for m in measures}) == len(measures):
        used = reserved | {m.name for m in measures}
        for m in measures:
            if m.name in reserved:
                base = m.metric or 'measure'
                candidate, index = base + '_value', 2
                while candidate in used:
                    candidate, index = f'{base}_value_{index}', index + 1
                renamed[m.name] = candidate
                used.add(candidate)
        measures = [m.model_copy(update={'name': renamed.get(m.name, m.name),
                    'ratio': [renamed.get(r, r) for r in m.ratio] if m.ratio else None}) for m in measures]
    ranking = (spec.ranking.model_copy(update={'by': renamed.get(spec.ranking.by, spec.ranking.by),
               'partition_by': list(dict.fromkeys(dimension_aliases.get(d, d) for d in spec.ranking.partition_by))})
               if spec.ranking else None)
    return spec.model_copy(update={"filters": filters, "measures": measures, 'dimensions': dimensions,
                                   'ranking': ranking, 'shares': [renamed.get(n, n) for n in spec.shares]})


def canonical(spec: QuerySpec) -> str:
    """What the spec computes, independent of names and order: two readings agree if this is equal."""
    def filt(f):
        return (f.field, f.op, sorted(map(str, f.values)), f.other)

    def measure(m):
        return (m.metric, m.agg, m.field, sorted(filt(f) for f in m.where), tuple(m.ratio or ()))
    by_name = {m.name: measure(m) for m in spec.measures}
    ratio_fixed = [(m.metric, m.agg, m.field, sorted(filt(f) for f in m.where),
                    tuple(json.dumps(by_name.get(r), default=str) for r in (m.ratio or ())))
                   for m in spec.measures]
    return json.dumps({
        "table": spec.table, "measures": sorted(json.dumps(x, default=str) for x in ratio_fixed),
        "dimensions": sorted(spec.dimensions),
        "filters": sorted(json.dumps(filt(f), default=str) for f in spec.filters),
        "time": spec.time.model_dump(mode="json") if spec.time else None,
        "ranking": [json.dumps(by_name.get(spec.ranking.by), default=str), spec.ranking.order,
                    spec.ranking.limit, sorted(spec.ranking.partition_by)]
        if spec.ranking else None,
        "shares": sorted(json.dumps(by_name.get(s), default=str) for s in spec.shares),
        "columns": sorted(spec.columns), "limit": spec.limit,
        **({"operation": spec.operation.model_dump(mode="json")} if spec.operation else {}),
    }, sort_keys=True, default=str)


def misspelt_values(question: str, spec: QuerySpec, cat: Catalog, aliases: dict[str, str]) -> list[tuple[str, str]]:
    """(word in the question, stored value) for filter values the question never names but nearly spells:
    "Mumbay" read as Mumbai. The user confirms the correction instead of the model applying it silently."""
    text = question.casefold()
    words = re.findall(r"[\w'-]+", text)
    out = []
    for f in spec.filters + [w for m in spec.measures for w in m.where]:
        if f.op not in ("eq", "in"):
            continue
        for value in f.values:
            if not isinstance(value, str) or len(value) < 4:
                continue
            names = [value.casefold()] + [a for a, v in aliases.items() if v == value]
            if any(n in text for n in names):
                continue
            size = len(value.split())
            grams = [" ".join(words[i:i + size]) for i in range(len(words) - size + 1)]
            close = max(grams, key=lambda g: SequenceMatcher(None, g, value.casefold()).ratio(), default="")
            if close and SequenceMatcher(None, close, value.casefold()).ratio() >= 0.8:
                out.append((close, value))
    return out


ALL_GROUPS = 30


def complete_ranking(question: str, spec: QuerySpec, cat: Catalog) -> QuerySpec:
    """"What do customers contact support about most?" names no count: a default top 10 of 16 issue categories
    silently drops six of them. With no number in the question and a split of at most ALL_GROUPS members, rank
    every group (still sorted, most first)."""
    r = spec.ranking
    if r is None or r.partition_by or len(spec.dimensions) != 1 or spec.time and spec.time.grain \
            or re.search(r"\b\d+\b|\b(?:one|two|three|four|five|ten|twenty)\b", question, re.I):
        return spec
    try:
        expression, _, entry = cat.resolve(spec.dimensions[0], spec.table)
        members = entry.labels() if entry is not None and entry.expression else cat.values_of(expression)
    except Exception:                                  # noqa: BLE001 - validation reports it
        return spec
    if not members or not r.limit < len(members) <= ALL_GROUPS:
        return spec
    return spec.model_copy(update={"ranking": r.model_copy(update={"limit": len(members)})})


COMPARE = r"\b(?:versus|vs\.?|compared? (?:with|to)|against|between)\b"


def compared_members(question: str, spec: QuerySpec, cat: Catalog, aliases: dict[str, str]) -> QuerySpec:
    """"COD versus prepaid" split by payment type must not be filtered to COD alone: when a single-member filter
    sits on a split the question compares, the filter becomes the members the question names (or goes, when they
    are all the members)."""
    if not re.search(COMPARE, question, re.I) or not spec.dimensions:
        return spec

    def said(value: str) -> bool:
        names = [value] + [a for a, v in aliases.items() if v == value]
        return any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", question, re.I) for n in names)
    try:
        split = {cat.resolve(d, spec.table)[0] for d in spec.dimensions}
    except Exception:                                  # noqa: BLE001 - validation reports it
        return spec
    filters = []
    for f in spec.filters:
        try:
            expression, _, entry = cat.resolve(f.field, spec.table)
        except Exception:                              # noqa: BLE001 - validation reports it
            filters.append(f)
            continue
        if f.op in ("eq", "in") and len(f.values) == 1 and expression in split:
            members = entry.labels() if entry is not None and entry.expression else cat.values_of(expression) or []
            named = [m for m in members if said(str(m))]
            if len(named) >= 2:
                if set(named) < set(members):
                    filters.append(f.model_copy(update={"op": "in", "values": named}))
                continue
        filters.append(f)
    return spec.model_copy(update={"filters": filters})


GENERIC = {"marketing", "product", "customer", "sales", "avg", "pct", "total", "share", "the", "of", "by"}


def _stems(name: str) -> set[str]:
    """'late_delivery_rate' -> {'late', 'deliv', 'rate'}: name words cut to 5 letters, generic words dropped."""
    return {w[:5] for w in re.split(r"[_.\s]+", name.split(".")[-1].casefold()) if w and w not in GENERIC}


def _mentioned(name: str, question: str) -> bool:
    words = {w[:5] for w in re.findall(r"[a-z]+", question.casefold())}
    stems = _stems(name)
    return bool(stems) and stems <= words


def _milestone(field: str | None, question: str) -> bool:
    """Does the question name the event a date column records? 'sales.delivered_at' needs "delivered", not
    "delivery address"; 'return_requested_at' is named by "returned" or "requested"."""
    if not field:
        return False
    words = [w for w in field.split(".")[-1].removesuffix("_at").split("_") if w not in GENERIC]
    forms = {w for w in words} | {w.removesuffix("ed").removesuffix("e") + "ed" for w in words} | {
        "returned" if w == "return" else w for w in words}
    return any(re.search(rf"\b{re.escape(f)}\b", question, re.I) for f in forms if len(f) > 3)


def immaterial(question: str, spec: QuerySpec, alt: QuerySpec) -> str | None:
    """Why an alternative reading is not worth asking about, or None if it is. Deterministic filters for what the
    contrast prompt is told not to propose: presentation changes and readings that drop the question's own words."""
    a, b = json.loads(canonical(spec)), json.loads(canonical(alt))
    diff = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
    if diff <= {"measures", "shares"} and (set(a["measures"]) <= set(b["measures"])
                                          or set(b["measures"]) <= set(a["measures"])):
        return "only adds or drops a supporting measure"
    if diff == {"time"} and a["time"] and b["time"] and {**a["time"], "field": None} == {**b["time"], "field": None} \
            and not _milestone(alt.time.field, question):
        return "dates by another milestone the question does not mention"
    if diff == {"filters"}:
        added = {f.field for f in alt.filters} - {f.field for f in spec.filters}
        kept = all(any(f == g for g in alt.filters) for f in spec.filters)
        if kept and added and all(f.op == "is_set" and not _milestone(f.field, question)
                                  for f in alt.filters if f.field in added):
            return "only requires a milestone the question does not mention"
    governed = {m.metric for m in spec.measures if m.metric}
    named = {m for m in governed if _mentioned(m, question)}
    if named - {m.metric for m in alt.measures}:
        return "replaces a metric the question names"
    if governed and not governed & {m.metric for m in alt.measures} and all(m.metric is None for m in alt.measures):
        return "replaces a governed metric with an ad-hoc aggregate"
    removed, added = set(spec.dimensions) - set(alt.dimensions), set(alt.dimensions) - set(spec.dimensions)
    if removed and added and all(not _mentioned(d, question) for d in added) and any(
            _mentioned(d, question) for d in removed):
        return "replaces a split the question names with one it does not"
    return None


def register_questions(question: str, register: dict[str, list[str]], answers: dict[str, str]) -> list:
    """Known two-meaning phrases in the question that the user's answers have not settled."""
    asked = " ".join(answers).casefold()
    out = []
    for phrase, meanings in register.items():
        if re.search(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", question, re.I) and phrase.casefold() not in asked:
            out.append(ClarifyingQuestion(question=f"What do you mean by '{phrase}'?", options=meanings,
                                          default=meanings[0], why="the two meanings give different numbers"))
    return out


def premise_notes(spec: QuerySpec | None, data: dict, start: date | None = None, end: date | None = None) -> list[str]:
    """Facts the user must know about the question's premise (stated in the read-back and the answer). Takes a
    spec, or explicit bounds (a template call's analysis period)."""
    notes = []
    first, last = date.fromisoformat(str(data["first_day"])[:10]), date.fromisoformat(str(data["last_day"])[:10])
    if spec is not None and spec.time:
        start, end = spec.time.start, spec.time.end
    if start or end:
        start, end = start or first, end or last
        if end < first or start > last:
            notes.append(f"The data covers {first} to {last}; nothing is recorded in the requested period, so the "
                         "result is empty for that period.")
        elif start < first or end > last:
            notes.append(f"The data covers {first} to {last}; the result covers {max(start, first)} to "
                         f"{min(end, last)}, the observed part of the requested period.")
    return notes


def reading_options(readings: list[QuerySpec], cat: Catalog) -> list[str]:
    """One short option per reading: only the read-back lines that differ between the readings."""
    lines = [readback(r, cat).replace("**", "").split("\n") for r in readings]
    shared = set.intersection(*(set(x) for x in lines))
    options = [" · ".join(line for line in x if line not in shared) or " · ".join(x) for x in lines]
    return options if len(set(options)) == len(options) else [" · ".join(x) for x in lines]
