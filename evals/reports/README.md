# Evaluation evidence

The repository keeps the **pinned raw evaluation JSON** needed to reproduce historical
accuracy, paired bootstrap comparisons, and failure taxonomy, plus these concise
methodological reports: original ablation, structured grounding, retrieval, Pulse, the
latest limited holdout, and the regression failure report.

Duplicate per-run Markdown summaries were intentionally removed. New full-evaluation
reports are **not checked into Git**: run the manual GitHub workflow
`Live agent evaluation (manual, paid)` and download its artifacts, which record
the code SHA, snapshot manifest and per-question results.

The 31-question original holdout (2026-11-15) is not the same as the later
five-question partial check (2026-11-16). The holdout after failure-driven
tuning is not an independent unseen evaluation. Independent stakeholder
questions in `../holdout_external.yaml` are currently **zero**.

Do not delete the retained raw JSON merely to make the repository smaller:
it is the audit trail for the claims summarized in `../../README.md`.
