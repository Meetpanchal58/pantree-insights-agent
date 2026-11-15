# End-to-end evaluation 2026-11-15 15:27

Data: full shop · model: openai:gpt-6.1-terra · temperature: provider default (not settable for this model) · variant(s): retrieval · repeats: 1

Format: mean [95% bootstrap CI over questions] (n questions, k repeats, pass^k = correct in all k).

## Holdout

| Measure | Result |
|---|---|
| Correct result tables (dimension-aware, 1%) | 81% [65%-94%] (n=31) |
| Correct answers (result + claims verified against gold) | 81% [65%-94%] (n=31) |
| Correct result tables, excluding questions used for tuning | 92% [81%-100%] (n=26) |
| SQL execution success | 97% [90%-100%] (n=31) |
| Grounded answers (all claims verified in the app) | 100% [100%-100%] (n=30) |
| Clarification accuracy (vague questions) | - |
| Decline accuracy (unsafe requests) | - |
| Adversarial declines (paraphrased injection / PII) | - |
| False-decline rate (valid questions declined) | - |
| Unnecessary clarification rate (clear questions, no defaults) | - |
| Retry / validator recovery (correct after >1 attempt) | 50% [0%-100%] (n=4) |
| Declines caught by input guard / planner | 0 / 0 |
| Median latency · mean cost per question | 15 s · $0.010 |

Failure types: wrong_dimension 4, missing_rows 1, sql_error 1

Failures:
- r0 x01 [missing_rows] "What was realized revenue in each region of the delivery address in 20": missing rows (empty result)
- r0 x03 [wrong_dimension] "How many orders did Prime and non-Prime customers place in 2026?": wrong dimension: no column matches 'is_prime'
- r0 y01 [wrong_dimension] "How many units did we sell in each product category in 2025?": wrong dimension: no column matches 'category'
- r0 y10 [sql_error] "What is the RTO rate in each city tier?": no answer (failed: Could not produce a valid query after 3 attempts (safety checks): Exactly one statement is allowed, f
- r0 y14 [wrong_dimension] "What is the average order value for Prime versus non-Prime customers?": wrong dimension: no column matches 'is_prime'
- r0 y16 [wrong_dimension] "What share of delivered orders in each region arrived late?": wrong dimension: no column matches 'region'

