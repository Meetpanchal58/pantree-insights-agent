# Fresh evaluation results

| Suite | N (all) | Correct answer tables (95% CI) | Verified = gold (95% CI) | Silent wrong | Correctly clarified (95% CI) | Correctly declined (95% CI) | Avg clarifying Qs | p50 / p95 seconds | Cost/question |
| --- | ---: | --- | --- | ---: | --- | --- | ---: | --- | --- |
| all | 100 | 27/56 · 48.2% [35.7%–60.8%] | 27/34 · 79.4% [64.7%–91.2%] | 7 | 5/6 · 83.3% [50.0%–100.0%] | 9/12 · 75.0% [50.0%–100.0%] | 0.39 | 6.851 / 27.283 | $0.00665 (96/100 known) |
| coverage | 14 | 5/14 · 35.7% [14.2%–57.2%] | 5/5 · 100.0% [56.5%–100.0%] | 0 | — (n=0) | — (n=0) | 0.65 | 7.048 / 28.501 | $0.00604 (14/14 known) |
| flow | 4 | 2/3 · 66.6% [0.0%–100.0%] | 2/2 · 100.0% [34.2%–100.0%] | 0 | 0/1 · 0.0% [0.0%–79.4%] | — (n=0) | 0.25 | 5.619 / 17.860 | $0.00828 (4/4 known) |
| holdout | 57 | 10/18 · 55.5% [33.3%–77.8%] | 10/11 · 90.9% [72.7%–100.0%] | 1 | 3/3 · 100.0% [43.8%–100.0%] | 7/10 · 70.0% [40.0%–90.0%] | 0.41 | 7.223 / 25.245 | $0.00627 (55/57 known) |
| regression | 25 | 10/21 · 47.6% [28.5%–71.5%] | 10/16 · 62.5% [37.5%–81.3%] | 6 | 2/2 · 100.0% [34.2%–100.0%] | 2/2 · 100.0% [34.2%–100.0%] | 0.24 | 4.830 / 28.274 | $0.00764 (23/25 known) |

N includes every attempted case. Correctness uses answer cases only; routing categories have their own denominators.
Clarification counts include unanswered questions and exclude confirmation prompts. Costs with missing usage are not treated as zero.
Point percentages are rounded down; interval bounds are rounded outward. Intervals bootstrap questions; all-pass/all-fail boundaries use Wilson intervals.
Cells with fewer than 30 cases have small N. One repeat does not measure run-to-run stability.
Latency excludes analyst preparation, browser/network time, and human confirmation time. Calls share provider and SQL caches.
These are simulated-data results. Narrative faithfulness and causal correctness are not measured.

Planned: 100; recorded: 100. Budget stopped: False.
Known cost: $0.637558; held for uncertain calls: $0.000000; ceiling: $4.00.
The ledger includes billed or uncertain calls even when an answer did not finish. Its total takes precedence over row-level cost averages.

## Free compiler baseline

294/294 gold-spec results match gold SQL across 98 cases × 3 passes. Model calls: 0.
DuckDB p50 / p95: 0.0074 / 0.1461 seconds. Snapshot unchanged: True.
This checks compilation from intended specifications, not natural-language understanding.

## Failure analysis

- **unresolved (20)**: `holdout/y11` — How many customers signed up through each acquisition channel in 2026? Reason: Requested clarification; autonomous mode supplies no business-choice answers.
- **over_asked (11)**: `holdout/f14` — Which pincode-level delivery regions have the most late deliveries? Region level is fine. Reason: asked a clarifying question
- **silent_wrong (7)**: `regression/h05` — Which campaigns get the most last-click credited revenue? Reason: missing rows for 'campaign_id'
