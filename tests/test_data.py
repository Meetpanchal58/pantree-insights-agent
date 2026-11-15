"""Checks against the real (small) shop: views, lockdown, metrics, examples and the pulse."""
import shutil

import pytest

from pantree_insights.config import ROOT
from pantree_insights.db import QueryTimeout, run_query
from pantree_insights.guardrails import check_sql
from pantree_insights.semantic import load_examples, load_metrics, load_schema

pytestmark = pytest.mark.data


def test_every_allowed_table_has_a_view_without_blocked_columns(con):
    schema = load_schema()
    for table, info in schema.tables.items():
        columns = [row[0] for row in con.execute(f"DESCRIBE {table}").fetchall()]
        assert columns == list(info.columns), table
    blocked = schema.blocked.column_names()
    for table in schema.tables:
        assert not blocked & {row[0] for row in con.execute(f"DESCRIBE {table}").fetchall()}


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv('/etc/passwd')",
    "COPY (SELECT 1) TO '/tmp/pantree_out.csv'",
    "SET enable_external_access = true",
    "ATTACH '/tmp/pantree_other.db'",
])
def test_connection_is_locked_down(con, sql):
    with pytest.raises(Exception):  # noqa: B017 - any DuckDB error means it was refused
        con.execute(sql)


@pytest.mark.parametrize("name", list(load_metrics().metrics))
def test_metric_runs_and_passes_guardrails(con, name):
    metrics = load_metrics()
    sql = metrics.sql(name)
    assert check_sql(sql).ok
    value = run_query(con, sql).frame.iloc[0, 0]
    assert value is not None
    breakdown = next(iter(metrics.sources[metrics.metrics[name].source].dimensions))
    assert len(run_query(con, metrics.sql(name, by=breakdown)).frame) > 0


@pytest.mark.parametrize("name", list(load_metrics().custom_metrics))
def test_custom_metric_runs(con, name):
    sql = load_metrics().custom_metrics[name].sql
    assert check_sql(sql).ok, check_sql(sql).errors
    assert len(run_query(con, sql).frame) > 0


@pytest.mark.parametrize("example", load_examples().examples, ids=lambda e: e.id)
def test_example_passes_guardrails_and_returns_rows(con, example):
    result = check_sql(example.sql)
    assert result.ok, result.errors
    assert len(run_query(con, result.sql).frame) > 0


def test_realized_revenue_definition(con):
    """Refunded lines keep (almost) nothing; realized revenue < net sales < gross sales."""
    m = load_metrics()
    values = {n: run_query(con, m.sql(n)).frame.iloc[0, 0] for n in ["gross_sales", "net_sales", "realized_revenue"]}
    assert values["realized_revenue"] < values["net_sales"] < values["gross_sales"]


def test_query_timeout(con):
    slow = "SELECT COUNT(*) FROM impressions a JOIN impressions b ON a.position = b.position"
    with pytest.raises(QueryTimeout):
        run_query(con, slow, timeout_s=0.5)


def test_rows_are_capped(con):
    result = run_query(con, "SELECT product_id FROM product_master", max_rows=10)
    assert len(result.frame) == 10 and result.truncated


def test_pulse_runs(shop_dir, tmp_path):
    from pantree_insights.pulse import run

    (tmp_path / "kpis").mkdir()
    shutil.copy(ROOT / "memory" / "kpis" / "kpi_definitions.yaml", tmp_path / "kpis")
    summary = run(shop_dir, tmp_path)
    assert summary["series"] > 50
    assert (tmp_path / "kpis" / "daily_kpis.parquet").exists()
    assert (tmp_path / "gaps" / "open.json").exists()
    again = run(shop_dir, tmp_path)               # idempotent: same data, no new gaps
    assert again["opened"] == 0 and again["open"] == summary["open"]


def test_db_layer_whole_row_cannot_leak_blocked_columns(con):
    """Even if a whole-row expression got past the guard, the view has no blocked columns."""
    row = con.execute("SELECT to_json(a) FROM customer_addresses a LIMIT 1").fetchone()[0]
    assert "address_line" not in row and "city" in row
    columns = [r[0] for r in con.execute("DESCRIBE SELECT * FROM customer_addresses").fetchall()]
    assert "address_line" not in columns


def test_db_layer_cannot_read_files_outside_the_shop(con):
    with pytest.raises(Exception):  # noqa: B017
        con.execute("SELECT * FROM read_parquet('/etc/passwd')").fetchall()
