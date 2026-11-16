# End-to-end evaluation 2026-11-16 20:05

Data: full shop · model: openai:gpt-6.1-terra · temperature: provider default (not settable for this model) · variant(s): full · repeats: 1

Format: mean [95% bootstrap CI over questions] (n questions, k repeats, pass^k = correct in all k).

## Regression

| Measure | Result |
|---|---|
| Correct result tables (dimension-aware, 1%) | 70% [48%-87%] (n=23) |
| Correct answers (result + claims verified against gold) | 70% [48%-87%] (n=23) |
| Correct result tables, excluding questions used for tuning | 70% [48%-87%] (n=23) |
| SQL execution success | 100% [100%-100%] (n=23) |
| Grounded answers (all claims verified in the app) | 100% [100%-100%] (n=23) |
| Clarification accuracy (vague questions) | - |
| Decline accuracy (unsafe requests) | - |
| Adversarial declines (paraphrased injection / PII) | - |
| False-decline rate (valid questions declined) | - |
| Unnecessary clarification rate (clear questions, no defaults) | - |
| Retry / validator recovery (correct after >1 attempt) | 67% [0%-100%] (n=3) |
| Declines caught by input guard / planner | 0 / 0 |
| Median latency · mean cost per question | 26 s · $0.026 |

Failure types: wrong_metric_value 4, missing_rows 2, wrong_dimension 1

Failures:
- r0 e02 [wrong_metric_value] "How many products are on sale right now?": wrong metric value: 'active_products' does not match (value, denominator or category)
- r0 m05 [wrong_metric_value] "Which return reasons are most common?": wrong metric value: 'share' does not match (value, denominator or category)
- r0 m08 [wrong_metric_value] "Which SKUs of active products are out of stock right now?": wrong metric value: 'inbound_qty' does not match (value, denominator or category)
- r0 m12 [missing_rows] "How much revenue comes from new versus repeat customers?": missing rows for 'customer_type'
- r0 h02 [wrong_metric_value] "How did Diwali 2026 do compared with Diwali 2025?": wrong metric value: 'realized_revenue' does not match (value, denominator or category)
- r0 h03 [missing_rows] "How much did each sale event lift daily net sales compared with the tw": missing rows for 'name'
- r0 h05 [wrong_dimension] "Which campaigns get the most last-click credited revenue?": wrong dimension: no column matches 'campaign_id'

