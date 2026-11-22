# Fresh evaluation results

| Suite | N (all) | Correct answer tables (95% CI) | Verified = gold (95% CI) | Silent wrong | Correctly clarified (95% CI) | Correctly declined (95% CI) | Avg clarifying Qs | p50 / p95 seconds | Cost/question |
| --- | ---: | --- | --- | ---: | --- | --- | ---: | --- | --- |
| all | 100 | 49/56 · 87.5% [78.5%–94.7%] | 49/51 · 96.0% [90.1%–100.0%] | 2 | 5/6 · 83.3% [50.0%–100.0%] | 12/12 · 100.0% [75.7%–100.0%] | 0.09 | 19.414 / 29.396 | $0.00913 (94/100 known) |
| coverage | 14 | 13/14 · 92.8% [78.5%–100.0%] | 13/14 · 92.8% [78.5%–100.0%] | 1 | — (n=0) | — (n=0) | 0.00 | 23.422 / 29.396 | $0.01109 (14/14 known) |
| flow | 4 | 2/3 · 66.6% [0.0%–100.0%] | 2/2 · 100.0% [34.2%–100.0%] | 0 | 0/1 · 0.0% [0.0%–79.4%] | — (n=0) | 0.25 | 5.558 / 17.663 | $0.00876 (4/4 known) |
| holdout | 57 | 16/18 · 88.8% [72.2%–100.0%] | 16/16 · 100.0% [80.6%–100.0%] | 0 | 3/3 · 100.0% [43.8%–100.0%] | 10/10 · 100.0% [72.2%–100.0%] | 0.08 | 20.050 / 27.695 | $0.00854 (53/57 known) |
| regression | 25 | 18/21 · 85.7% [71.4%–100.0%] | 18/19 · 94.7% [84.2%–100.0%] | 1 | 2/2 · 100.0% [34.2%–100.0%] | 2/2 · 100.0% [34.2%–100.0%] | 0.16 | 17.809 / 30.425 | $0.00937 (23/25 known) |

N includes every attempted case. Correctness uses answer cases only; routing categories have their own denominators.
Clarification counts include unanswered questions and exclude confirmation prompts. Costs with missing usage are not treated as zero.
Point percentages are rounded down; interval bounds are rounded outward. Intervals bootstrap questions; all-pass/all-fail boundaries use Wilson intervals.
Cells with fewer than 30 cases have small N. One repeat does not measure run-to-run stability.
Latency excludes analyst preparation, browser/network time, and human confirmation time. Calls share provider and SQL caches.
These are simulated-data results. Narrative faithfulness and causal correctness are not measured.

Planned: 100; recorded: 100. Budget stopped: False.
Known cost: $0.867386; held for uncertain calls: $0.000000; ceiling: $4.00.
The ledger includes billed or uncertain calls even when an answer did not finish. Its total takes precedence over row-level cost averages.

## Failure analysis

- **unresolved (3)**: `flow/flow-context` — Now split those orders by payment method, keeping that same city and all available dates. Reason: Requested clarification; autonomous mode supplies no business-choice answers.
- **declined_wrongly (2)**: `holdout/o-z01` — How many orders were placed in December 2024? Reason: declined
- **silent_wrong (2)**: `coverage/c24` — How was paid ad spend split across targeted regions in 2026, and what click-through rate did the ads get in each? Reason: missing rows for 'region'
