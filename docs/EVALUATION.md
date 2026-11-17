# Evaluation

Pantree keeps evaluation inputs and scoring code in Git, but generated run reports are CI/local artifacts rather than source files.

## Suites

- **Holdout:** author-written questions with independently reviewed gold SQL.
- **Regression:** verified examples with their own retrieval example hidden.
- **Behaviour:** valid sensitive-looking questions, unsafe requests and prompt-injection paraphrases.
- **Flow regression:** review-derived edge cases; never reported as unseen accuracy.
- **External holdout:** questions written by people outside the project. Model-written questions are never labeled external.

## Reproduce

```bash
uv run python evals/run_eval.py --suite all --dry-run
uv run python evals/retrieval_eval.py
```

A paid full run is available through the manual **Live model evaluation** GitHub Action. It pins the commit in the artifact metadata and uploads raw reports as workflow artifacts instead of committing them.

## External questions

Collect questions verbatim:

```bash
uv run python evals/collect_external.py --author <name> --question "<question>"
```

Review intent, required tables/metrics, result grain and gold SQL independently before moving a question from `external_intake.yaml` to `holdout_external.yaml`.

The repository intentionally ships with no fabricated external accuracy claim.
