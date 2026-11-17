# Evaluation

How the agent is evaluated before production and stakeholder presentation. Earlier runs were made on older
architectures and have been removed; results are added here only from runs of the current code.

## What is tested (148 questions)

| Suite | File | Questions | What a pass means |
|---|---|---|---|
| Holdout answers | `holdout.yaml` | 31 | Result table matches independently written gold SQL (dimension-aware, 1% tolerance) |
| Holdout clarify | `holdout.yaml` | 4 | A vague question gets a clarifying question, not a guess |
| Holdout decline | `holdout.yaml` | 5 | Personal data, raw dumps, injection and off-topic requests are declined |
| False declines | `behaviour.yaml` | 16 | Valid questions that sound sensitive ("delivery region", "email campaigns") are answered |
| Adversarial | `behaviour.yaml` | 11 | Paraphrased attacks are declined |
| Over-asking | holdout answers without defaults | 31 | A clear question is understood without questions (a read-back confirmation is fine) |
| Regression | `semantic/examples.yaml` + `questions.yaml` | 44 | Same as holdout; each question's own example is hidden from retrieval |
| Conversation flow | `flow_regression.yaml` | 6 | Follow-up questions use the chat history |

Holdout questions are never in examples, prompts, the query library or the knowledge base:
`tests/test_eval_leakage.py` and `tests/test_library.py` check wording and results. Questions whose failures
informed a fix are marked `tuned_on` and reported separately. External questions (written by other people from
`EXTERNAL_QUESTIONS.md`) go in `holdout_external.yaml` and are reported by author.

## Free checks first

```bash
uv run pytest -q
```

```bash
DATA_SIZE=full uv run python evals/run_eval.py --suite all --dry-run
```

```bash
DATA_SIZE=full uv run python evals/retrieval_eval.py
```

```bash
DATA_SIZE=full uv run python evals/library_coverage.py --run
```

## Production evaluation (paid, about US$3.5-4)

One pass over all 148 questions, then a second pass over the 31 holdout answers for consistency (pass^2).
`--max-cost-usd` stops the run cleanly at the cap; provider errors are retried once and listed, never scored.

```bash
DATA_SIZE=full uv run python evals/run_eval.py --suite all --max-cost-usd 4 --minimum-pass-rate 0.90 --yes
```

```bash
DATA_SIZE=full uv run python evals/run_eval.py --suite holdout --sets answer --repeat-offset 1 --max-cost-usd 1 --yes
```

Or run the **Live agent evaluation** workflow in GitHub Actions (manual; repeats and cost cap are inputs); its
reports are uploaded as an artifact.

Estimated cost per question (gpt-6.1-terra): about US$0.025 for a full answer, about US$0.006 when the run stops
early (decline, clarifying question, read-back).

## Reading the report

Each run writes `reports/e2e_<suite>_<variant>_r<first>-<last repeat>_<time>.md` and the per-question JSON. Measures are
reported as mean [95% bootstrap CI over questions]; pass^k is the share of questions correct in all k repeats;
failures are listed by type (wrong dimension, wrong metric value, missing or extra rows, declined wrongly, not
declined, over-asked, did not clarify, SQL error). `--rescore` re-scores a saved run for free after a scoring fix.
