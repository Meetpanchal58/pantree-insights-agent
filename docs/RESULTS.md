# Results

This file keeps only the benchmark summary needed to understand the project. Raw JSON/Markdown runs are generated locally or by GitHub Actions and are not committed.

## Historical benchmark (before this reliability PR)

Full Pantree shop, 15 November 2026, `gpt-6.1-terra`:

| Evaluation | Result |
|---|---:|
| Internal answer holdout | 28/31 = 90% correct result tables, repeated 3 times |
| Baseline (schema dump; no planner/validator) | 48% |
| Retrieval-only variant | 81% |
| Full system | 90% |
| Initial regression suite | 31/38 = 82% |
| Later regression rerun after targeted fixes | 21/30 = 70% |
| Unsafe/adversarial declines in the historical run | 16/16 |
| External holdout | Not yet populated |

The historical holdout is useful evidence, but it is **not** claimed as the score of this PR. Several architecture changes happened afterwards, and some holdout failures informed fixes.

## Current release gate

Before describing a new revision as more accurate, run the manual **Live model evaluation** workflow on the exact commit and retain its uploaded artifact. Recommended release evidence:

- full holdout + regression + behaviour + flow suites;
- 3 repeats for answer questions;
- separately reported external holdout;
- result-table accuracy and narrative-claim accuracy;
- clarification/false-decline/safety rates;
- latency, retries and cost.

See [EVALUATION.md](EVALUATION.md).
