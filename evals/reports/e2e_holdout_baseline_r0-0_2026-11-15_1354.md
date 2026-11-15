# End-to-end evaluation 2026-11-15 15:27

Data: full shop · model: openai:gpt-6.1-terra · temperature: provider default (not settable for this model) · variant(s): baseline · repeats: 1

Format: mean [95% bootstrap CI over questions] (n questions, k repeats, pass^k = correct in all k).

## Holdout

| Measure | Result |
|---|---|
| Correct result tables (dimension-aware, 1%) | 48% [32%-68%] (n=31) |
| Correct answers (result + claims verified against gold) | 48% [32%-68%] (n=31) |
| Correct result tables, excluding questions used for tuning | 54% [35%-73%] (n=26) |
| SQL execution success | 87% [74%-97%] (n=31) |
| Grounded answers (all claims verified in the app) | 100% [100%-100%] (n=27) |
| Clarification accuracy (vague questions) | - |
| Decline accuracy (unsafe requests) | - |
| Adversarial declines (paraphrased injection / PII) | - |
| False-decline rate (valid questions declined) | - |
| Unnecessary clarification rate (clear questions, no defaults) | - |
| Retry / validator recovery (correct after >1 attempt) | 17% [0%-42%] (n=12) |
| Declines caught by input guard / planner | 0 / 0 |
| Median latency · mean cost per question | 17 s · $0.013 |

Failure types: wrong_dimension 6, sql_error 4, wrong_metric_value 4, extra_rows 1, missing_rows 1

Failures:
- r0 x01 [sql_error] "What was realized revenue in each region of the delivery address in 20": no answer (failed: Could not produce a valid query after 3 attempts (safety checks): Exactly one statement is allowed, f
- r0 x02 [sql_error] "What is the return rate for each apparel sub-category?": no answer (failed: Could not produce a valid query after 3 attempts (safety checks): Exactly one statement is allowed, f
- r0 x03 [wrong_metric_value] "How many orders did Prime and non-Prime customers place in 2026?": wrong metric value: 'orders' does not match (value, denominator or category)
- r0 x04 [wrong_metric_value] "What is the average order value by device in 2026?": wrong metric value: 'aov' does not match (value, denominator or category)
- r0 x06 [sql_error] "What did one ad click cost on each paid channel in 2025?": no answer (failed: Could not produce a valid query after 3 attempts (safety checks): Exactly one statement is allowed, f
- r0 x08 [sql_error] "What share of Footwear reviews are negative, by review aspect?": no answer (failed: Could not produce a valid query after 3 attempts (safety checks): Exactly one statement is allowed, f
- r0 x10 [wrong_dimension] "Which 5 brands brought the most realized revenue in 2026?": wrong dimension: no column matches 'brand_name'
- r0 x11 [wrong_metric_value] "What is the cancellation rate for COD versus prepaid orders?": wrong metric value: 'cancellation_rate' does not match (value, denominator or category)
- r0 y01 [wrong_dimension] "How many units did we sell in each product category in 2025?": wrong dimension: no column matches 'category'
- r0 y04 [wrong_dimension] "What is the return rate for women's products versus men's products?": wrong dimension: no column matches 'gender'
- r0 y07 [extra_rows] "How many support tickets were opened each month in 2026?": extra rows for 'month'
- r0 y09 [wrong_metric_value] "What is the gross margin for each product usage type such as casual, f": wrong metric value: 'gross_margin_pct' does not match (value, denominator or category)
- r0 y10 [wrong_dimension] "What is the RTO rate in each city tier?": wrong dimension: no column matches 'city_tier'
- r0 y12 [wrong_dimension] "What is the average discount off MRP for each brand of footwear, for t": wrong dimension: no column matches 'brand_name'
- r0 y14 [missing_rows] "What is the average order value for Prime versus non-Prime customers?": missing rows (empty result)
- r0 y16 [wrong_dimension] "What share of delivered orders in each region arrived late?": wrong dimension: no column matches 'region'

