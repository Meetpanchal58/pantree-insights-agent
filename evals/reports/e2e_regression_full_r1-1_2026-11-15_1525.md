# End-to-end evaluation 2026-11-15 15:27

Data: full shop · model: openai:gpt-6.1-terra · temperature: provider default (not settable for this model) · variant(s): full · repeats: 2

Format: mean [95% bootstrap CI over questions] (n questions, k repeats, pass^k = correct in all k).

## Regression

| Measure | Result |
|---|---|
| Correct result tables (dimension-aware, 1%) | 70% [53%-83%] (n=30) |
| Correct answers (result + claims verified against gold) | 70% [53%-83%] (n=30) |
| Correct result tables, excluding questions used for tuning | 70% [53%-83%] (n=30) |
| SQL execution success | 100% [100%-100%] (n=30) |
| Grounded answers (all claims verified in the app) | 100% [100%-100%] (n=30) |
| Clarification accuracy (vague questions) | - |
| Decline accuracy (unsafe requests) | - |
| Adversarial declines (paraphrased injection / PII) | - |
| False-decline rate (valid questions declined) | - |
| Unnecessary clarification rate (clear questions, no defaults) | - |
| Retry / validator recovery (correct after >1 attempt) | 75% [25%-100%] (n=4) |
| Declines caught by input guard / planner | 0 / 0 |
| Median latency · mean cost per question | 23 s · $0.018 |

Failure types: wrong_metric_value 3, missing_rows 2, extra_rows 2, wrong_dimension 2, api_error 1

Failures:
- r1 m02 [api_error] "Monthly realized revenue and orders over time": provider error after retry (not scored)
- r1 m05 [wrong_metric_value] "Which return reasons are most common?": wrong metric value: 'share' does not match (value, denominator or category)
- r1 m08 [wrong_metric_value] "Which SKUs of active products are out of stock right now?": wrong metric value: 'inbound_qty' does not match (value, denominator or category)
- r1 m12 [missing_rows] "How much revenue comes from new versus repeat customers?": missing rows for 'customer_type'
- r1 h02 [wrong_metric_value] "How did Diwali 2026 do compared with Diwali 2025?": wrong metric value: 'realized_revenue' does not match (value, denominator or category)
- r1 h03 [missing_rows] "How much did each sale event lift daily net sales compared with the tw": missing rows for 'name'
- r1 h07 [extra_rows] "How do ad platforms' reported orders compare with orders we recorded, ": extra rows for 'channel'
- r1 h09 [wrong_dimension] "Which product sizes lose the most sales to stock-outs?": wrong dimension: no column matches 'category'
- r1 h10 [extra_rows] "Do customers who get the wrong size return more in footwear or apparel": extra rows for 'category'
- r1 n01 [wrong_dimension] "How deep are discounts on sale-event days compared with normal days?": wrong dimension: no column matches 'day_type'

