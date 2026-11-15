# End-to-end evaluation 2026-11-15 14:21

Data: full shop · model: openai:gpt-6.1-terra · temperature: provider default (not settable for this model) · variant(s): full · repeats: 1

Format: mean [95% bootstrap CI over questions] (n questions, k repeats, pass^k = correct in all k).

## Regression

| Measure | Result |
|---|---|
| Correct result tables (dimension-aware, 1%) | 74% [58%-87%] (n=38) |
| Correct answers (result + claims verified against gold) | 71% [55%-84%] (n=38) |
| Correct result tables, excluding questions used for tuning | 74% [58%-87%] (n=38) |
| SQL execution success | 97% [92%-100%] (n=38) |
| Grounded answers (all claims verified in the app) | 100% [100%-100%] (n=37) |
| Clarification accuracy (vague questions) | 67% [0%-100%] (n=3) |
| Decline accuracy (unsafe requests) | 100% [100%-100%] (n=3) |
| Adversarial declines (paraphrased injection / PII) | - |
| False-decline rate (valid questions declined) | - |
| Unnecessary clarification rate (clear questions, no defaults) | - |
| Retry / validator recovery (correct after >1 attempt) | 100% [100%-100%] (n=1) |
| Declines caught by input guard / planner | 2 / 1 |
| Median latency · mean cost per question | 21 s · $0.015 |

Failure types: extra_rows 3, wrong_metric_value 2, wrong_dimension 2, missing_rows 2, claims_unverified 1, declined_wrongly 1, did_not_clarify 1

Failures:
- r0 e05 [claims_unverified] "What share of customers are Prime members?": claims: c1 (10.2): result has 0.102012 for value of prime_share
- r0 m05 [wrong_metric_value] "Which return reasons are most common?": wrong metric value: 'share' does not match (value, denominator or category)
- r0 m08 [wrong_dimension] "Which SKUs of active products are out of stock right now?": wrong dimension: no column matches 'category'
- r0 m09 [wrong_dimension] "How much did we spend on each marketing channel?": wrong dimension: no column matches 'channel_group'
- r0 m11 [declined_wrongly] "What do customers contact support about most, and how satisfied are th": declined by input_guard
- r0 h03 [missing_rows] "How much did each sale event lift daily net sales compared with the tw": missing rows for 'name'
- r0 h05 [extra_rows] "Which campaigns get the most last-click credited revenue?": extra rows for 'campaign_id'
- r0 h07 [extra_rows] "How do ad platforms' reported orders compare with orders we recorded, ": extra rows for 'channel'
- r0 h09 [wrong_metric_value] "Which product sizes lose the most sales to stock-outs?": wrong metric value: 'lost_share' does not match (value, denominator or category)
- r0 n01 [missing_rows] "How deep are discounts on sale-event days compared with normal days?": missing rows for 'day_type'
- r0 n05 [extra_rows] "Which ship cities have the highest RTO rate?": extra rows for 'ship_city'
- r0 c02 [did_not_clarify] "Is marketing working?": did not clarify

