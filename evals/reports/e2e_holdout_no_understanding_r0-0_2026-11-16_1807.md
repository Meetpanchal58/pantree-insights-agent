# End-to-end evaluation 2026-11-16 20:05

Data: full shop · model: openai:gpt-6.1-terra · temperature: provider default (not settable for this model) · variant(s): no_understanding · repeats: 1

Format: mean [95% bootstrap CI over questions] (n questions, k repeats, pass^k = correct in all k).

## Holdout

| Measure | Result |
|---|---|
| Correct result tables (dimension-aware, 1%) | 100% [100%-100%] (n=31) |
| Correct answers (result + claims verified against gold) | 94% [84%-100%] (n=31) |
| Correct result tables, excluding questions used for tuning | 100% [100%-100%] (n=26) |
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

Failure types: claims_unverified 2

Failures:
- r0 x03 [claims_unverified] "How many orders did Prime and non-Prime customers place in 2026?": claims: c1 (8,212): "0 rows match [('is_prime', 'Prime')]"; c2 (32,931): "0 rows match [('is_prime', 'Non-Prime')]"; c3 
- r0 y14 [claims_unverified] "What is the average order value for Prime versus non-Prime customers?": claims: c1 (1,793): "0 rows match [('is_prime', 'Prime')]"; c2 (1,777): "0 rows match [('is_prime', 'Non-Prime')]"; c3 (

