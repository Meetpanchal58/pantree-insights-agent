# Fresh evaluation results

| Suite | N (all) | Correct answer tables (95% CI) | Verified = gold (95% CI) | Silent wrong | Correctly clarified (95% CI) | Correctly declined (95% CI) | Avg clarifying Qs | p50 / p95 seconds | Cost/question |
| --- | ---: | --- | --- | ---: | --- | --- | ---: | --- | --- |
| all | 100 | 47/56 · 83.9% [75.0%–92.9%] | 47/52 · 90.3% [82.6%–98.1%] | 5 | 5/6 · 83.3% [50.0%–100.0%] | 12/12 · 100.0% [75.7%–100.0%] | 0.09 | 19.488 / 30.808 | $0.00875 (92/100 known) |
| coverage | 14 | 12/14 · 85.7% [64.2%–100.0%] | 12/13 · 92.3% [76.9%–100.0%] | 1 | — (n=0) | — (n=0) | 0.00 | 22.476 / 30.584 | $0.01142 (13/14 known) |
| flow | 4 | 2/3 · 66.6% [0.0%–100.0%] | 2/2 · 100.0% [34.2%–100.0%] | 0 | 0/1 · 0.0% [0.0%–79.4%] | — (n=0) | 0.25 | 5.229 / 20.029 | $0.00477 (4/4 known) |
| holdout | 57 | 16/18 · 88.8% [72.2%–100.0%] | 16/16 · 100.0% [80.6%–100.0%] | 0 | 3/3 · 100.0% [43.8%–100.0%] | 10/10 · 100.0% [72.2%–100.0%] | 0.08 | 18.681 / 27.345 | $0.00812 (52/57 known) |
| regression | 25 | 17/21 · 80.9% [61.9%–95.3%] | 17/21 · 80.9% [61.9%–95.3%] | 4 | 2/2 · 100.0% [34.2%–100.0%] | 2/2 · 100.0% [34.2%–100.0%] | 0.16 | 19.490 / 32.613 | $0.00935 (23/25 known) |

N includes every attempted case. Correctness uses answer cases only; routing categories have their own denominators.
Clarification counts include unanswered questions and exclude confirmation prompts. Costs with missing usage are not treated as zero.
Point percentages are rounded down; interval bounds are rounded outward. Intervals bootstrap questions; all-pass/all-fail boundaries use Wilson intervals.
Cells with fewer than 30 cases have small N. One repeat does not measure run-to-run stability.
Latency excludes analyst preparation, browser/network time, and human confirmation time. Calls share provider and SQL caches.
These are simulated-data results. Narrative faithfulness and causal correctness are not measured.

Planned: 100; recorded: 100. Budget stopped: False.
Known cost: $0.820696; held for uncertain calls: $0.132121; ceiling: $4.00.
The ledger includes billed or uncertain calls even when an answer did not finish. Its total takes precedence over row-level cost averages.

## Failure analysis

- **silent_wrong (5)**: `coverage/c24` — How was paid ad spend split across targeted regions in 2026, and what click-through rate did the ads get in each? Reason: missing rows for 'region'
- **execution_error (4)**: `holdout/f08` — How many customers opted in to email and to SMS? Reason: execution_error
- **declined_wrongly (2)**: `holdout/o-z01` — How many orders were placed in December 2024? Reason: declined
