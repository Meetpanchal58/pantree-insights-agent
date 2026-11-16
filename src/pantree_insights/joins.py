"""Join paths built in code from the approved joins (schema.yaml) and their measured cardinality (profile.py).

The SQL writer no longer has to recall join conditions: given the table the question is about (the root)
and the other tables it needs, `JoinGraph.path` returns the joins that connect them, adding bridge tables
when needed (marketing_spend -> campaigns -> sale_events). Joins that keep one row per root row (N:1, 1:1)
cost 1; joins that can multiply root rows (1:N, N:N) cost 10, so they are used only when no safe route
exists, and are flagged so the writer aggregates that side first. Join rules (schema.yaml) add their extra
condition to the ON clause.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass

from .profile import Profile
from .semantic import Schema

SAFE, FAN_OUT = 1, 10


@dataclass(frozen=True)
class JoinStep:
    left: str                         # table already in the query
    right: str                        # table joined in
    join: str                         # the approved join as written in schema.yaml
    on: str                           # full ON condition (with any join-rule extra)
    fans_out: bool                    # joining `right` can repeat `left` rows

    def sql(self) -> str:
        return f"JOIN {self.right} ON {self.on}"


class JoinGraph:
    def __init__(self, schema: Schema, profile: Profile):
        self.schema = schema
        self.edges: dict[str, list[JoinStep]] = {t: [] for t in schema.tables}
        for join in schema.joins:
            (lt, lc), (rt, rc) = (side.strip().split(".") for side in join.split("="))
            measured = profile.joins.get(join)
            left_many = measured is None or measured.left_per_key > 1
            right_many = measured is None or measured.right_per_key > 1
            rule = schema.join_rules.get(join)
            on = f"{lt}.{lc} = {rt}.{rc}" + (f" AND {rule.extra}" if rule else "")
            # walking left -> right repeats left rows if one left key has several right rows
            self.edges[lt].append(JoinStep(lt, rt, join, on, fans_out=right_many))
            self.edges[rt].append(JoinStep(rt, lt, join, on, fans_out=left_many))

    def _shortest(self, start: list[str], target: str) -> list[JoinStep]:
        """Cheapest route from any table in `start` to `target` (Dijkstra). Ties go to the earlier start table
        (the root first), then to the order of joins in schema.yaml."""
        queue = [(0, i, t, []) for i, t in enumerate(start)]
        heapq.heapify(queue)
        seen: set[str] = set()
        counter = len(queue)
        while queue:
            cost, _, table, route = heapq.heappop(queue)
            if table == target:
                return route
            if table in seen:
                continue
            seen.add(table)
            for step in self.edges[table]:
                if step.right not in seen:
                    counter += 1
                    heapq.heappush(queue, (cost + (FAN_OUT if step.fans_out else SAFE), counter, step.right,
                                           route + [step]))
        raise KeyError(f"no approved join path to {target}")

    def path(self, root: str, tables: list[str]) -> list[JoinStep]:
        """Joins connecting `root` to every table in `tables` (in that order), each table joined once."""
        for t in [root, *tables]:
            if t not in self.edges:
                raise KeyError(f"unknown or blocked table: {t}")
        joined, steps = [root], []
        for target in tables:
            if target in joined:
                continue
            for step in self._shortest(joined, target):
                if step.right not in joined:
                    steps.append(step)
                    joined.append(step.right)
        return steps

    def render(self, root: str, tables: list[str]) -> str:
        """FROM/JOIN lines for the SQL writer, with a warning for each join that can repeat rows."""
        steps = self.path(root, tables)
        lines = [f"FROM {root}"] + [s.sql() for s in steps]
        warnings = [f"- {s.right} can repeat {s.left} rows: aggregate {s.right} in its own CTE first, "
                    f"then join the aggregate" for s in steps if s.fans_out]
        rules = [f"- {s.join}: {self.schema.join_rules[s.join].why}" for s in steps if s.join in self.schema.join_rules]
        return "\n".join(lines + (["Row multiplication:"] + warnings if warnings else [])
                         + (["Join rules:"] + rules if rules else []))
