# End-to-end evaluation 2026-11-15 15:27

Data: full shop · model: openai:gpt-6.1-terra · temperature: provider default (not settable for this model) · variant(s): full · repeats: 1

Format: mean [95% bootstrap CI over questions] (n questions, k repeats, pass^k = correct in all k).

## Regression

| Measure | Result |
|---|---|
| Correct result tables (dimension-aware, 1%) | 82% [68%-92%] (n=38) |
| Correct answers (result + claims verified against gold) | 79% [66%-92%] (n=38) |
| Correct result tables, excluding questions used for tuning | 82% [68%-92%] (n=38) |
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

Failure types: wrong_metric_value 4, declined_wrongly 1, missing_rows 1, extra_rows 1, claims_unverified 1, did_not_clarify 1

Failures:
- r0 m05 [wrong_metric_value] "Which return reasons are most common?": wrong metric value: 'share' does not match (value, denominator or category)
- r0 m11 [declined_wrongly] "What do customers contact support about most, and how satisfied are th": declined by input_guard
- r0 h03 [missing_rows] "How much did each sale event lift daily net sales compared with the tw": missing rows for 'name'
- r0 h05 [wrong_metric_value] "Which campaigns get the most last-click credited revenue?": wrong metric value: 'last_click_revenue' does not match (value, denominator or category)
- r0 h07 [extra_rows] "How do ad platforms' reported orders compare with orders we recorded, ": extra rows for 'channel'
- r0 h09 [wrong_metric_value] "Which product sizes lose the most sales to stock-outs?": wrong metric value: 'lost_share' does not match (value, denominator or category)
- r0 n01 [wrong_metric_value] "How deep are discounts on sale-event days compared with normal days?": wrong metric value: 'avg_discount_pct' does not match (value, denominator or category)
- r0 n05 [claims_unverified] "Which ship cities have the highest RTO rate?": claims: c10 (2.6%): "0 rows match [('ship_city', 'Bengaluru')]"
- r0 c02 [did_not_clarify] "Is marketing working?": did not clarify

