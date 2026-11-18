"""Independent checks for calculation defects found while establishing the baseline."""
import duckdb
import pytest

from pantree_insights.guardrails import check_sql
from pantree_insights.interpret import readback
from pantree_insights.spec import Filter, Measure, QuerySpec, compile_spec, default_catalog, validate
from pantree_insights.templates import build


@pytest.mark.data
@pytest.mark.parametrize("metric", ["orders", "net_sales", "realized_revenue", "cancellation_rate", "rto_rate", "aov"])
def test_named_metric_population_filters_apply_to_every_aggregate(con, metric):
    cat = default_catalog()
    # Include an unfiltered measure to catch implementations that incorrectly filter the entire query.
    spec = QuerySpec(table="sales", measures=[
        Measure(name="city_value", metric=metric, where=[Filter(field="city", values=["Mumbai"])]),
        Measure(name="all_orders", metric="orders")])
    assert not validate(spec, cat)
    sql = compile_spec(spec, cat)
    assert check_sql(sql).ok
    actual = con.execute(sql).fetchone()
    expected = con.execute(cat.metrics.sql(metric) + " AND s.ship_city = 'Mumbai'").fetchone()[0]
    all_orders = con.execute("SELECT COUNT(DISTINCT order_id) FROM sales WHERE line_type='Sale'").fetchone()[0]
    assert actual == pytest.approx((expected, all_orders))
    assert "Mumbai" in readback(spec, cat)


@pytest.mark.data
@pytest.mark.parametrize("festival_only", [False, True])
def test_overlapping_event_filters_do_not_duplicate_revenue(con, festival_only):
    names = [r[0] for r in con.execute("SELECT DISTINCT name FROM sale_events").fetchall()]
    spec = QuerySpec(table="sales", measures=[Measure(name="revenue", metric="realized_revenue")],
                     filters=[Filter(field="sale_event", values=names)])
    if festival_only:
        spec.filters.append(Filter(field="sale_events.event_type", values=["Festival"]))
    sql = compile_spec(spec, default_catalog())
    assert "EXISTS" in sql and check_sql(sql).ok
    predicate = " AND e.event_type = 'Festival'" if festival_only else ""
    expected = con.execute("""SELECT SUM(CASE WHEN delivered_at IS NOT NULL THEN net_amount
        - COALESCE(refund_amount, 0) ELSE 0 END) FROM sales s WHERE line_type='Sale'
        AND EXISTS (SELECT 1 FROM sale_events e WHERE s.placed_at >= e.start_date
                    AND s.placed_at < e.end_date + INTERVAL 1 DAY""" + predicate + ")").fetchone()[0]
    assert con.execute(sql).fetchone()[0] == pytest.approx(expected)


@pytest.mark.data
def test_ratio_filters_must_not_be_silently_ignored():
    spec = QuerySpec(table="sales", measures=[Measure(name="orders", metric="orders"),
        Measure(name="ratio", ratio=["orders", "orders"], where=[Filter(field="city", values=["Mumbai"])])])
    assert any("numerator and denominator" in p for p in validate(spec, default_catalog()))


def test_event_daily_sales_include_zero_days_and_exclude_incomplete_windows():
    with duckdb.connect() as con:
        con.execute("""CREATE TABLE sales AS SELECT CAST(day AS TIMESTAMP) AS placed_at, amount AS net_amount,
            NULL::TIMESTAMP AS cancelled_at, kind AS line_type FROM (VALUES
            ('2026-01-01',100,'Sale'), ('2026-01-02',100,'Sale'), ('2026-01-03',100,'Sale'),
            ('2026-01-05',0,'Sale'), ('2026-02-01',999,'Exchange')) t(day,amount,kind)""")
        con.execute("""CREATE TABLE sale_events AS SELECT id AS event_id, id AS name,
            CAST(start AS DATE) AS start_date, CAST(finish AS DATE) AS end_date FROM (VALUES
            ('half','2026-01-03','2026-01-04'), ('zero','2026-01-04','2026-01-05'),
            ('incomplete_before','2026-01-02','2026-01-03'),
            ('incomplete_after','2026-01-05','2026-01-06')) t(id,start,finish)""")
        sql, _ = build("event_lift", {"days_before": 2})
        assert check_sql(sql).ok
        rows = {r[0]: r for r in con.execute(sql).fetchall()}
        assert set(rows) == {"half", "zero"}
        assert rows["half"][4:] == (50.0, 100.0, -0.5)
        assert rows["zero"][4:] == (0.0, 100.0, -1.0)
