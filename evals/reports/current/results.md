# Fresh evaluation results

| Suite | N (all) | Correct answer tables (95% CI) | Verified = gold (95% CI) | Silent wrong | Correctly clarified (95% CI) | Correctly declined (95% CI) | Avg clarifying Qs | p50 / p95 seconds | Cost/question |
| --- | ---: | --- | --- | ---: | --- | --- | ---: | --- | --- |
| all | 100 | 51/56 · 91.0% [83.9%–98.3%] | 51/53 · 96.2% [90.5%–100.0%] | 2 | 6/6 · 100.0% [60.9%–100.0%] | 12/12 · 100.0% [75.7%–100.0%] | 0.09 | 19.724 / 29.952 | $0.00966 (96/100 known) |
| coverage | 14 | 13/14 · 92.8% [78.5%–100.0%] | 13/14 · 92.8% [78.5%–100.0%] | 1 | — (n=0) | — (n=0) | 0.00 | 22.346 / 28.994 | $0.01376 (14/14 known) |
| flow | 4 | 2/3 · 66.6% [0.0%–100.0%] | 2/2 · 100.0% [34.2%–100.0%] | 0 | 1/1 · 100.0% [20.6%–100.0%] | — (n=0) | 0.50 | 4.647 / 19.057 | $0.00450 (4/4 known) |
| holdout | 57 | 18/18 · 100.0% [82.4%–100.0%] | 18/18 · 100.0% [82.4%–100.0%] | 0 | 3/3 · 100.0% [43.8%–100.0%] | 10/10 · 100.0% [72.2%–100.0%] | 0.06 | 20.186 / 29.434 | $0.00899 (55/57 known) |
| regression | 25 | 18/21 · 85.7% [71.4%–100.0%] | 18/19 · 94.7% [84.2%–100.0%] | 1 | 2/2 · 100.0% [34.2%–100.0%] | 2/2 · 100.0% [34.2%–100.0%] | 0.16 | 18.210 / 30.657 | $0.00965 (23/25 known) |

N includes every attempted case. Correctness uses answer cases only; routing categories have their own denominators.
Clarification counts include unanswered questions and exclude confirmation prompts. Costs with missing usage are not treated as zero.
Point percentages are rounded down; interval bounds are rounded outward. Intervals bootstrap questions; all-pass/all-fail boundaries use Wilson intervals.
Cells with fewer than 30 cases have small N. One repeat does not measure run-to-run stability.
Latency excludes analyst preparation, browser/network time, and human confirmation time. Calls share provider and SQL caches.
These are simulated-data results. Narrative faithfulness and causal correctness are not measured.

Planned: 100; recorded: 100. Budget stopped: False.
Known cost: $0.926527; held for uncertain calls: $0.000000; ceiling: $2.00.
The ledger includes billed or uncertain calls even when an answer did not finish. Its total takes precedence over row-level cost averages.

## Free compiler baseline

294/294 gold-spec results match gold SQL across 98 cases × 3 passes. Model calls: 0.
DuckDB p50 / p95: 0.0077 / 0.0703 seconds. Snapshot unchanged: True.
This checks compilation from intended specifications, not natural-language understanding.

## Failure analysis

- **unresolved (3)**: `flow/flow-context` — Now split those orders by payment method, keeping that same city and all available dates. Reason: Requested clarification; autonomous mode supplies no business-choice answers.
- **silent_wrong (2)**: `regression/m11` — What do customers contact support about most, and how satisfied are they? Reason: wrong metric value: 'tickets' does not match (value, denominator or category)
- **failed (1)**: `holdout/f05` — How many distinct customers placed orders in 2026? I only need the count, no customer names. Reason: not completed (failed)
