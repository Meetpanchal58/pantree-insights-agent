# Refactoring audit and validation

Reviewed against `main` at `030d2ed` (2026-11-22), not the qualitative scores in the request. The supplied request
ends mid-sentence in section 6. This change preserves LangGraph, semantic definitions, the deterministic SQL
compiler, reviewed templates, authentication, scoped memory, offline demos and SQL guardrails. It adds no runtime
dependency and does not claim that the aspirational architecture scores have been measured or achieved.

## Measured evidence

- Unchanged baseline: **1,479 passed, 1 skipped** in 96.37 s.
- Refactored full suite: **1,519 passed, 1 skipped** in 88.64 s; four additional regression tests were then added
  and included in the final focused checks. The skip is an existing offline-demo snapshot check: its pinned
  fixture differs from the nightly simulator data. These whole-suite times are not controlled latency benchmarks.
- Compiler evaluation: **98/98** answer cases equal independent gold SQL (94 compiled specs, four templates).
- Gold validation: **129 queries**, **zero problems**, across 172 evaluation items.
- Chromium/Playwright frontend suite: passed, including immediate result delivery before the final answer,
  stream disconnect recovery, offline demos, charts, exports and view races. Shell/JavaScript syntax and Ruff pass.
- Reviewed question benchmark: **15/15 independent SQL matches**, **zero model calls**. Five questions, three
  passes, fresh conversational memory per run; shared Agent SQL/OS caches and local disk. Median 51.7 ms,
  maximum 408.1 ms; Agent preparation 594.5 ms, excluded from request timing. This is not Cloud Run latency.
- Offline context ablation: 28 named metrics, 56,256-character full catalog. Of 66 cases requiring named metrics,
  keyword top-five retrieved all required metrics in 53 cases, top-ten in 58; BM25 top-five/top-ten in 61.
  Full context contained all required metrics in all 66. These figures measure metric recall, not answer accuracy.

Raw benchmark outputs: [fast path](validation/fastpath-baseline.json),
[context retrieval](validation/context-retrieval.json). They include the parent commit and source-content digest.
The public small dataset manifest is dated 2026-11-22, seed 42. Models were scripted or absent in these checks.
No OpenAI API key was available; no paid/live-model evaluation, remote PostgreSQL validation or Cloud Run
measurement was performed. This draft is not a deployment approval or a claim of complete production validation.

## Findings against the supplied audit

| # | Finding | Verified status and response |
|---|---|---|
| 1 | Structurally valid misinterpretation | Confirmed. Added independent explicit metric/dimension/grain checks and full-match reviewed grammar. General language understanding still needs live evaluation. |
| 2 | Compilation cannot establish intent | Confirmed. Verification scope is explicit; model-path read-back and material clarification remain. |
| 3 | Single fact limitation | Confirmed. Named metrics now compile into up to four independently aggregated fact stages with safe post-aggregation ratios. |
| 4 | One-to-many rejection | Confirmed. Membership filters use EXISTS; multi-fact metrics use pre-aggregation. Ambiguous fan-out projections still fail. |
| 5 | Full catalog context | Confirmed. Reused stable context; retained full model context after measured lexical retrieval misses. Reviewed grammar needs no prompt. |
| 6 | Sequential contrast and alternate execution | Confirmed. Contrast is skipped on the reviewed path; all speculative SQL execution removed. |
| 7 | Equal results treated as semantic agreement | Confirmed. Alternatives are compared structurally and against explicit intent, independently of coincidental values. |
| 8 | Column-insensitive, rounded/truncated comparison | Confirmed. Removed that comparison and its dead helper entirely. |
| 9 | Template verification bypass | Partial. Templates already had SQL safety. Added unsupported-parameter and date-scope checks; arbitrary template/question equivalence remains a model/read-back limitation. |
| 10 | Unnecessary confirmation | Confirmed. Reviewed context-free KPI questions execute without confirmation; unsupported modifiers and prior context fall back. |
| 11 | Weak semantic/units/causal grounding | Confirmed. Added unit checks, multiple vs percent handling, duplicate/non-finite claim checks, partial-population restrictions, text-surface coverage and causal-language rejection. Arbitrary prose semantics remain unproved. |
| 12 | Rendering model claim values | Confirmed. Accepted claims are replaced with recomputed values before rendering. |
| 13 | Stakeholder evaluation masking first-turn failure | Partially addressed in the existing baseline. Added all-answer first-turn plan and completion measures before any assistance, including automatic answers. |
| 14 | Cold starts | Still deployment-dependent. No unmeasured cold-start speedup claimed. |
| 15 | Startup cardinality scans | Confirmed on cache miss. Upload now publishes versioned measured join metadata; startup loads matching artifacts, with safe measurement fallback. |
| 16 | Pulse competes at startup | Confirmed. Removed from web startup; documented separate maintenance execution and publication of saved pulse artifacts. |
| 17 | Excess model calls | Three-call old ordinary path confirmed by baseline test. Reviewed context-free path uses zero; complex model path retains bounded repair/verification. |
| 18 | Per-run model clients | Confirmed. Provider clients are cached across runs; cost trackers remain separate. |
| 19 | Duplicate memory/context reads | Confirmed. Memory fetched once per request and retained across clarification; catalog prompt assembly cached. |
| 20 | Remote persistence round trips | Persistent checkpoints retained. Removed duplicate memory retrieval; made usage/code operations atomic. Remote latency is unmeasured. |
| 21 | GCS-mounted Parquet | Confirmed. Retained pending deployment-specific local-copy/ephemeral-storage benchmarks; local disk tests cannot justify a production claim. |
| 22 | Fixed small resource allocation | Confirmed. CPU, memory, minimum instances and HTTP concurrency are configurable; defaults/billing unchanged. |
| 23 | Process-local runs/queues/events | Confirmed. Added bounded admission, atomic same-chat start/resume, terminal checkpoint cleanup and bounded completed histories. Active distributed execution is still unsupported; max instances remains one. |
| 24 | Late numerical result delivery | Confirmed. Checked result event precedes analysis; frontend displays the table immediately. |
| 25 | Redundancy/obsolete paths | Removed result-based semantic comparison, repeated provider construction, duplicate context fetch and startup pulse work; updated stale architecture/deployment claims. Retained tested components and dependencies with actual callers. |

Additional fixes include a difference operation incorrectly evaluating division by zero, rounded/coarsened row
keys matching the wrong evidence, unrestricted small free-typed numbers, colliding SQL output aliases, invalid
ratio dependencies and non-finite filters. High-cardinality filter probes now use independent DuckDB cursors.
Email codes cannot be redeemed twice concurrently, failed attempts cannot lose increments, first-use budget
rows cannot race, and daily question admission is an atomic quota check plus increment.

## Supported analytical boundaries

Multi-fact plans use conformed concepts with matching expressions and identity keys. Date filters apply to each
metric's own governed date, and per-measure filters stay within their stage. Ranking occurs after aggregation;
missing populations remain NULL. Synthetic fixtures independently check duplicate fact rows, absent/null keys
and zero denominators. The existing metric-by-dimension and gold suites remain intact.

The compiler does not accept arbitrary model-written SQL, unsafe fact fan-out, raw multi-source aggregates,
ambiguous cross-source dates, or arbitrary cohort/funnel programs. Existing fixed-window repeat-purchase and
reconciliation templates remain supported. Unsupported analytical forms must be clarified or declined until
they have a governed definition and independently tested implementation.

The full catalog remains on the model path because the measured lexical alternatives lost required metrics.
Vector/hybrid retrieval and learned example retrieval were not introduced without credentials and a measured
end-to-end benefit. Existing scoped memory retrieval remains separate from schema retrieval; evaluation gold
specs are never inserted into production prompts.

## Remaining release work

1. Run the existing paid evaluation on the exact branch with OpenAI credentials. Inspect first-turn outcomes,
   eventual corrected outcomes, unsupported abstentions, costs and latency separately.
2. Exercise PostgreSQL checkpoint/transaction behavior and concurrent real requests in staging. SQLite atomicity
   tests and SQLAlchemy PostgreSQL syntax do not constitute a remote Postgres integration test.
3. Publish matching join metadata and pulse artifacts, then measure actual Cloud Run cold starts and GCS access.
   Check current pricing before changing resources. No deployment was performed here.
4. Before horizontal scaling, implement durable job leases, cross-instance admission/event replay and recovery
   of active work. Durable paused checkpoints alone are insufficient.
5. Expand reviewed grammar and complex plans only with new independent cases. Numerical verification and a
   causal-language filter do not fully verify every qualitative conclusion; the verification label states this limit.

Reproduce locally with `uv sync --frozen`, `pantree-data download --size small`, `pytest -q`, Ruff, the frontend
suite, `DATA_SIZE=small python evals/spec_eval.py`, `evals/fastpath_baseline.py` and `evals/context_retrieval.py`.
Use the repository's `.venv`/`uv run` for those commands. No new external service or deployment is required.
