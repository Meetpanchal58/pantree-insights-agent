# Knowledge retrieval and its evaluation

This adds an opt-in retrieval stage to the existing verified analysis agent. It does **not** replace the SQL
compiler, add arbitrary model-written SQL, prove 99% answer accuracy, or fix Cloud Run connection failures.
`RETRIEVAL_MODE=full` remains the default until a live answer comparison justifies changing it.

## What is indexed

`pantree-knowledge build` generates 566 records from the reviewed semantic YAML and analysis templates:
tables, individual columns, metric definitions, approved joins, subject-specific concepts, business terms,
ambiguities and reviewed multi-step analyses. Records have stable IDs, source references and dependencies.
Table records carry grain, population rules and date meanings; metric records include their source and
calculation. Retrieved dependencies are included together, never cut in the middle of a definition.

There is no second hand-maintained business dictionary. Changes to the records invalidate the index by a
SHA-256 fingerprint. Catalogue values are snapshot metadata, not proof that a value exists in today's data.
Runtime catalogue validation, value normalization, SQL checks and result verification remain authoritative.

The corpus excludes evaluation questions/answers, customer rows and user/project memory. Existing scoped
memory retrieval is unchanged. Recent chat context and clarification answers can inform a query, but are
never added to the shared index. A bounded cache stores only hashed query keys and embedding vectors.

## Search and deployment

1. BM25 searches names and terminology.
2. A local ONNX sentence encoder searches meaning. The initial model is
   [all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/tree/1110a243fdf4706b3f48f1d95db1a4f5529b4d41),
   pinned to commit `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, with 384-dimensional normalized mean-pooled vectors.
   It is a compact English baseline selected for measured CPU latency, not a claim of best-in-class multilingual
   retrieval. Model inputs truncate at 256 tokens; returned evidence preserves entire definitions.
3. Reciprocal-rank fusion combines the top 60 candidates from each search, with constant 60. Explicit named
   metric matches are preserved first. The keyword and vector baselines use the same name anchors.
4. Up to 12 seed records expand to their dependencies within a 24,000-character evidence budget. The small,
   fixed template interface is appended outside that evidence budget. No reranking model/API is called.

Modes:

| Mode | Behaviour |
|---|---|
| `full` (default) | Existing full-catalog prompt; no encoder/index loading or retrieval work. |
| `shadow` | Record retrieval IDs, timing and fallback status; still send the full catalog. |
| `hybrid` | Use retrieved definitions in interpretation, repair and contrast prompts. |

If the index is missing, stale, corrupt, mismatched to the model, empty, or exceeds the context budget, the
agent uses the full catalog. Retrieval exceptions also fall back. This fallback does **not** detect every
semantic omission; live evaluation is required. Only public definitions are shared between concurrent runs.

The Docker image downloads pinned model files and builds its index during image creation. The model card
is retained next to the weights. Serving never downloads weights or builds embeddings on a user's request.
The ONNX weights add about 90 MB before image compression; initialization uses one inference thread.
Changing semantic files requires rebuilding the index/image. No production setting is changed by this PR.

## Reproduce the free retrieval benchmark

```bash
uv sync --locked
HF_HUB_DISABLE_XET=1 uv run pantree-knowledge build --download-model
uv run pantree-knowledge inspect
uv run python evals/retrieval_eval.py --suite all --repeats 3 --gate
```

Only the explicit download command needs network access. Inference and evaluation run locally without an
OpenAI key. The benchmark uses existing required-table/metric/join labels, excludes unlabelled cases,
rejects unknown labels and uses the same seed and context budgets for each method. Query-vector caching is
disabled. Model/index startup and the first query are reported separately from shuffled warm timings.
JSON reports include per-question omissions, context sizes, overflow, model/weights/index/source/case hashes,
package versions, machine information and the working revision. GitHub Actions uploads JSON and Markdown.

The held-out gate requires at least 99% mean required-evidence recall, 95% complete-evidence questions,
zero budget overflow, and warm p95 at most 500 ms. Development-only suites cannot satisfy the release gate.
Fallback to the full catalog never counts as successful retrieval. This is a **retrieval gate only**.

Initial local result (18 November 2026): 60 labelled questions, including 31 held-out, repeated three times.
The corpus/ranking was adjusted using the five flow development questions before the held-out run.

| Method, same name anchors and budget | Required evidence recall | Complete questions | Warm p50 / p95 |
|---|---:|---:|---:|
| Keyword | 99.6% | 98.3% | 1.0 / 1.7 ms |
| Vector | 98.8% | 95.0% | 7.4 / 10.7 ms |
| Hybrid | 100% | 100% | 8.2 / 11.2 ms |

Hybrid held-out results: 100% labelled recall/completeness, p95 10.3 ms, no overflow. Model/index startup:
0.80 s. Average retrieved evidence: 14,798 characters. Index fingerprint:
`8b1838389179068577d0a25ff9665bb34e1f7d931bd43ffddf761025f679922a`.
Evidence: `retrieval_20261118T115935_732484Z.json`; subsequent CI runs publish their own artifacts.

These labels do not measure every required column, predicate, join cardinality, cohort/funnel meaning,
follow-up intent or final claim. Three repeats are 60 distinct questions, not 180 independent questions.
100% on this small benchmark cannot establish 99% accuracy in production. nDCG in JSON scores only direct
seed labels; its ranking can differ from complete-evidence scores after dependency expansion. Local warm
retrieval latency is not Cloud Run startup time, model response latency or a concurrency/load test.

## Next: compare actual answers before enabling hybrid

Use an immutable/versioned copy of the same data and the same provider model in separate processes. Start
with the flow suite; the following commands make paid calls and need a provider key in the environment:

```bash
DATA_SIZE=small RETRIEVAL_MODE=full DAILY_BUDGET_USD=5 uv run python evals/run_eval.py \
  --suite flow --repeats 3 --max-cost-usd 1 --output-dir evals/reports/full --gate --yes
DATA_SIZE=small RETRIEVAL_MODE=hybrid DAILY_BUDGET_USD=5 uv run python evals/run_eval.py \
  --suite flow --repeats 3 --max-cost-usd 1 --output-dir evals/reports/hybrid --gate --yes
```

The cost cap stops starting calls; one in-flight call may exceed it. Budget exhaustion is an incomplete
evaluation, not a pass. If flow passes, compare the full suites with an explicitly chosen sufficient budget.
Review exact result tables, every numeric claim, unsafe declines, unnecessary clarification, conversation
scope, overall p50/p95, token usage, cost and fallback rate. Provider errors stay in the denominator.
The reports record retrieval mode and a separate retrieval stage, including sources used on each turn.

Before broad promotion, add independently authored held-out cases with column/filter/date and multi-turn
labels, plus out-of-domain requests. The current benchmark alone is too small for the requested 99% target.
Test concurrency and interrupted streams separately on staging. Keep full mode available for rollback.
General model-written complex SQL needs its own validation and evaluation work; it is not enabled here.
