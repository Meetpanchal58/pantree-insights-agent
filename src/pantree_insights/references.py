"""Reference queries picked by structure.

Retrieval finds examples whose wording resembles the question. After the question is understood, we know its
structure: the tables, columns and metrics it needs. This index picks the verified examples and library queries
that use the same structure, so a question worded unlike any reference still gets the right SQL pattern.

Score = 3 per shared metric + 2 per shared column + 1 per shared table - 0.5 per extra table the reference
needs. A reference must share at least one table. Hidden examples (evaluation) are never picked.
"""
from __future__ import annotations

from .semantic import Example
from .sqlinfo import Usage, analyse


class ReferenceIndex:
    def __init__(self, examples: list[Example]):
        self.examples = {e.id: e for e in examples}
        self.usage: dict[str, Usage] = {e.id: analyse(e.sql) for e in examples}

    def pick(self, tables: set[str], columns: set[str], metrics: set[str], exclude: set[str] = frozenset(),
             k: int = 3) -> list[Example]:
        scored = []
        for eid, e in self.examples.items():
            u = self.usage[eid]
            if eid in exclude or not (u.tables & tables):
                continue
            score = 3 * len(metrics & set(e.metrics)) + 2 * len(columns & u.columns) + len(tables & u.tables) \
                - 0.5 * len(u.tables - tables)
            if score > 0:
                scored.append((score, eid))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [self.examples[eid] for _, eid in scored[:k]]

    @staticmethod
    def render(examples: list[Example]) -> str:
        return "\n\n".join(f"-- {e.question}\n{e.sql.strip()}" + (f"\n-- Note: {e.notes}" if e.notes else "")
                           for e in examples)
