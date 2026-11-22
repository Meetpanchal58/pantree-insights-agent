"""Interpret: readings."""
from __future__ import annotations

import json
import re
from datetime import date

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


def register_questions(question: str, register: dict[str, list[str]], answers: dict[str, str]) -> list:
    """Known two-meaning phrases in the question that the user's answers have not settled."""
    asked = " ".join(answers).casefold()
    out = []
    for phrase, meanings in register.items():
        if re.search(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", question, re.I) and phrase.casefold() not in asked:
            out.append(ClarifyingQuestion(question=f"What do you mean by '{phrase}'?", options=meanings,
                                          default=meanings[0], why="the two meanings give different numbers"))
    return out


def premise_notes(spec: QuerySpec, data: dict) -> list[str]:
    """Facts the user must know about the question's premise (stated in the read-back and the answer)."""
    notes = []
    first, last = date.fromisoformat(str(data["first_day"])[:10]), date.fromisoformat(str(data["last_day"])[:10])
    if spec.time and (spec.time.start or spec.time.end):
        start, end = spec.time.start or first, spec.time.end or last
        if end < first or start > last:
            notes.append(f"The data covers {first} to {last}; the requested period is unavailable. "
                         "Missing coverage cannot be interpreted as zero activity.")
        elif start < first or end > last:
            notes.append(f"The data covers {first} to {last}; only that part of the requested period is included.")
    return notes


def reading_options(readings: list[QuerySpec], cat: Catalog) -> list[str]:
    """One short option per reading: only the read-back lines that differ between the readings."""
    lines = [readback(r, cat).replace("**", "").split("\n") for r in readings]
    shared = set.intersection(*(set(x) for x in lines))
    options = [" · ".join(line for line in x if line not in shared) or " · ".join(x) for x in lines]
    return options if len(set(options)) == len(options) else [" · ".join(x) for x in lines]
