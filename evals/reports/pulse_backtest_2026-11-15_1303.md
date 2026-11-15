# Pulse backtest 2026-11-15 13:03 (small shop)

150 injected shocks (seed 7): one series, one 7-day window, numerator x 1.25, 1.5, 2.0, 0.75, 0.5. Detection at that week end. No LLM calls.

| Method | Recall (shock flagged) | False alerts per week (other unexplained flags) | Precision* | Sale-event weeks with net-sales rise flagged |
|---|---|---|---|---|
| legacy (mean/SD z>=2.5) | 35/150 (23%) | 1.17 | 17% | 10/49 |
| robust_bh (median/MAD + BH q=0.10) | 43/150 (29%) | 2.77 | 9% | 15/49 |

Recall by shock size:

| Factor | legacy (mean/SD z>=2.5) | robust_bh (median/MAD + BH q=0.10) |
|---|---|---|
| x1.25 | 3/26 | 5/26 |
| x1.5 | 10/27 | 12/27 |
| x2.0 | 11/30 | 13/30 |
| x0.75 | 7/34 | 7/34 |
| x0.5 | 4/33 | 6/33 |

*Precision treats every other unexplained flag as false; some are real anomalies in the simulated data, so this is a lower bound for both methods.
