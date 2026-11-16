# End-to-end evaluation 2026-11-16 18:07

Data: full shop · model: openai:gpt-6.1-terra · temperature: provider default (not settable for this model) · variant(s): no_understanding · repeats: 1

Format: mean [95% bootstrap CI over questions] (n questions, k repeats, pass^k = correct in all k).

## Holdout

| Measure | Result |
|---|---|
| Correct result tables (dimension-aware, 1%) | 94% [84%-100%] (n=31) |
| Correct answers (result + claims verified against gold) | 94% [84%-100%] (n=31) |
| Correct result tables, excluding questions used for tuning | 96% [88%-100%] (n=26) |
| SQL execution success | 100% [100%-100%] (n=31) |
| Grounded answers (all claims verified in the app) | 100% [100%-100%] (n=31) |
| Clarification accuracy (vague questions) | - |
| Decline accuracy (unsafe requests) | - |
| Adversarial declines (paraphrased injection / PII) | - |
| False-decline rate (valid questions declined) | - |
| Unnecessary clarification rate (clear questions, no defaults) | - |
| Retry / validator recovery (correct after >1 attempt) | 100% [100%-100%] (n=1) |
| Declines caught by input guard / planner | 0 / 0 |
| Median latency · mean cost per question | 23 s · $0.016 |

Failure types: wrong_dimension 2

Failures:
- r0 x03 [wrong_dimension] "How many orders did Prime and non-Prime customers place in 2026?": wrong dimension: no column matches 'is_prime'
- r0 y14 [wrong_dimension] "What is the average order value for Prime versus non-Prime customers?": wrong dimension: no column matches 'is_prime'

