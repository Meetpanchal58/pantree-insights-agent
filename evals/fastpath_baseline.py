"""No-model benchmark of reviewed questions, checked against independent SQL.

DATA_SIZE=small uv run python evals/fastpath_baseline.py
Latency excludes process/Agent startup and measures local disk, not Cloud Run or a provider.
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pandas as pd

from pantree_insights.config import get_settings
from pantree_insights.db import manifest
from pantree_insights.graph import Agent
from pantree_insights.llm import FakeLLM
from pantree_insights.memory import Memory

CASES = [
    ("What was yesterday's GMV?", """SELECT SUM(gross_amount) AS gross_sales FROM sales
        WHERE line_type='Sale' AND CAST(placed_at AS DATE)=DATE '{last}'-INTERVAL 1 DAY"""),
    ("What is the monthly return rate?", """SELECT date_trunc('month',placed_at) AS month,
        COUNT(*) FILTER (WHERE return_requested_at IS NOT NULL) * 1.0 /
        NULLIF(COUNT(*) FILTER (WHERE delivered_at IS NOT NULL),0) AS return_rate
        FROM sales WHERE line_type='Sale' GROUP BY 1 ORDER BY 1"""),
    ("Show revenue by category.", """SELECT p.category, SUM(CASE WHEN s.delivered_at IS NOT NULL
        THEN s.net_amount-COALESCE(s.refund_amount,0) ELSE 0 END) AS realized_revenue
        FROM sales s LEFT JOIN product_master p ON p.product_id=s.product_id
        WHERE s.line_type='Sale' GROUP BY 1 ORDER BY 2 DESC"""),
    ("Which brands generated the highest sales?", """SELECT p.brand_name AS brand,
        SUM(CASE WHEN s.delivered_at IS NOT NULL THEN s.net_amount-COALESCE(s.refund_amount,0)
        ELSE 0 END) AS realized_revenue FROM sales s LEFT JOIN product_master p ON p.product_id=s.product_id
        WHERE s.line_type='Sale' GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 10"""),
    ("Show sales for the last seven days.", """SELECT SUM(CASE WHEN delivered_at IS NOT NULL
        THEN net_amount-COALESCE(refund_amount,0) ELSE 0 END) AS realized_revenue FROM sales
        WHERE line_type='Sale' AND placed_at >= DATE '{last}'-INTERVAL 6 DAY
        AND placed_at < DATE '{last}'+INTERVAL 1 DAY"""),
]


def main():
    def unexpected(*args):
        raise AssertionError("reviewed question made a model call")

    started = time.perf_counter()
    agent = Agent(llm_factory=lambda: FakeLLM(unexpected))
    preparation = time.perf_counter() - started
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for repeat in range(3):
            for i, (question, reference) in enumerate(CASES):
                started = time.perf_counter()
                run_id, stream = agent.start(question, memory=Memory(Path(tmp) / f'{repeat}-{i}', agent.data['size']))
                events = list(stream)
                if events[-1].get("kind") == "confirm":
                    events += list(agent.resume(run_id, "Go ahead", events[-1]["plan_version"]))
                elapsed = time.perf_counter() - started
                final = events[-1]
                assert final['status'] == 'done' and final['grounded']
                assert final['cost']['calls'] == 0
                assert any(e['type'] == 'result' for e in events)
                got = pd.DataFrame(final['result']['data'], columns=final['result']['columns'])
                expected = agent.con.execute(reference.format(last=agent.data['last_day'])).df()
                for col in expected:
                    if pd.api.types.is_datetime64_any_dtype(expected[col]):
                        got[col] = pd.to_datetime(got[col])
                pd.testing.assert_frame_equal(got, expected, check_dtype=False, rtol=1e-10, atol=1e-8)
                rows.append({'question': question, 'repeat': repeat, 'seconds': elapsed,
                             'model_calls': 0, 'reference_matched': True})
    agent.close()
    print(json.dumps({'kind': 'reviewed_grammar_local', 'data': manifest(), 'preparation_seconds': preparation,
                      'cache': 'shared Agent SQL and OS caches; fresh conversational memory per request',
                      'data_size': get_settings().data_size, 'rows': rows}, indent=2))


if __name__ == '__main__':
    main()
