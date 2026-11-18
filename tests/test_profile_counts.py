"""The faster profile must preserve row-weighted coverage, duplicates and null handling."""
import duckdb
import pytest

from pantree_insights.profile import profile_join


@pytest.mark.parametrize(("left", "right", "expected"), [
    ([1, 1, 2, 3, None], [1, 1, 4, None], (2, 2, 0.5, 0.3333)),
    ([1, 2, 2], [1, 2], (2, 1, 0.0, 0.0)),
    ([], [1, 1], (0, 2, 0.0, 1.0)),
    ([None], [None], (0, 0, 0.0, 0.0)),
    ([1], [], (1, 0, 1.0, 0.0)),
])
def test_grouped_profile_preserves_cardinality_and_unmatched_rows(left, right, expected):
    with duckdb.connect() as con:
        for name, values in [("source_a", left), ("source_b", right)]:
            con.execute(f"CREATE TABLE {name}(id INTEGER)")
            if values:
                con.executemany(f"INSERT INTO {name} VALUES (?)", [(v,) for v in values])
        p = profile_join(con, "source_a.id = source_b.id")
        assert (p.left_per_key, p.right_per_key, p.left_unmatched, p.right_unmatched) == expected
