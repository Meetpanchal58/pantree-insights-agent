# Architecture

A model chooses an analytical request; governed Python code validates and compiles SQL. LangGraph retains
checkpointed clarification, deterministic execution, bounded repair, charting and memory. Verification describes
the selected scope and numeric evidence, not a proof of natural-language understanding or causality.

## Conditional flow

```text
input safety -> standing constraints -> intent
  reviewed complete KPI grammar or explicit edit of prior spec, no conflicting context -> validated QuerySpec
  otherwise -> interpreter -> structural + explicit intent checks -> at most one repair
    material ambiguity -> clarification checkpoint
    valid spec -> conditional semantic contrast (no speculative SQL) -> read-back confirmation
    valid template -> parameter and supported-scope checks -> read-back confirmation
  -> compile 1–4 proposals -> scope/coverage + SQL safety -> versioned conversational approval
  -> exact approved SQL (digest + version + snapshot checked at execution boundary)
  -> snapshot check -> immediate result event
  -> deterministic KPI summary or analyst -> numeric/unit/prose checks -> at most one rewrite
  -> remove unsupported claims or use deterministic fallback -> recompute values -> chart -> scoped memory
```

A grammar match consumes the whole question, including explicit uniquely resolved `where city/brand/category/channel
is ...` filters. Prior self-contained questions and old numeric observations do not block it. Explicit `And by
brand?` or `same for yesterday` edits preserve the previous successful plan. Unknown modifiers, arbitrary prior
instructions, user/project preferences and business profiles still require interpretation. Recent prose uses the last four turns. A separate per-chat investigation record retains the objective, origin
plan, current plan, metric definitions, and the last eight verified evidence summaries (six explicitly marked
example rows per query). It survives that prose window and PostgreSQL restarts; complete runs remain in chat
history. Independent objectives start a new context; refinements, drill-downs and comparisons preserve it.
Standing instructions belong in user/project preferences. Historical insights are fetched
only when needed. Relative dates follow the snapshot's last day and are disclosed. A known
ambiguous phrase is checked before either path. Unresolved interpretations stop after six clarification rounds.
No metric is invented for an unsupported concept such as an undefined NMV.

## Query planning

`spec.py` retains QuerySpec and compiles each named metric from `semantic/metrics.yaml`. A multi-fact spec may
combine named metrics from two to four sources and ratios of their results. Each stage aggregates at its native
grain, applies its own governed date and population rules, and then joins on conformed concept keys. Ranking
happens after the join. Missing populations stay NULL; zero denominators produce NULL. Shared dimension and
filter expressions must match across sources; same names with different meanings are refused. Row lists,
unreviewed aggregates and explicit cross-source dates are not allowed on this path.

One-to-many filters use correlated EXISTS when they only express membership. Unsupported projections across
fan-out joins still fail, rather than inflating measures. Measured join metadata is tied to the schema, profiler
implementation and published dataset manifest. The data publisher must update the manifest on every refresh.
`operations.py` reuses the compiler for whole-period comparisons and daily rolling/cumulative windows. Each
comparison recomputes its own distinct counts and ratio denominators; windows require explicitly time-additive
metrics. Windows use calendar days, include the required lookback and emit observed days only. Queries are
bounded to 367 output days, with no early ranking. Semi-additive snapshots and non-additive values cannot be
summed through time. Templates cover event lift, channel ROAS, reconciliation, repeat purchase, mature first-order
cohorts, strictly ordered session funnels and customer frequency/recency segments. Dates and steps are typed,
bounded parameters. Cohorts exclude immature buyers; funnels use first-step sessions as their denominator.
Unknown template parameters and unsupported date scope cannot silently disappear.

## Evidence and delivery

The database result is streamed before explanation. Model claims name a result column, row keys, an operation
and a unit. The verifier checks those, rejects duplicate IDs, incomplete-population arithmetic, non-finite
values, small ungrounded numbers and common unsupported causal assertions. It does not sum or average ratios
without their underlying numerators and denominators. Accepted approximations are replaced by recomputed
values before display. ROAS is a multiple, rather than a percentage. Chart text and follow-up text are also checked.

Explicit direction and extremum claims require matching evidence in the same sentence. Every requested numeric
measure with observed values must appear in the summary; a failed rewrite falls back to deterministic evidence.
These controls are not a complete theorem prover for prose. For example, arbitrary qualitative conclusions
and unrecognised causal paraphrases remain a limitation of the model explanation path. Fast-path summaries
use deterministic cell descriptions and make no causal claims. Model failures, unanswerable scope and material
ambiguity are separate outcomes; unsafe input and SQL remain blocked.

## Context and runtime

The full catalog is kept for the model path. An offline ablation of 66 named-metric cases found missing required
metrics in 13 cases with keyword top-five retrieval and five cases with BM25 top-five or top-ten retrieval.
This is metric recall, not model accuracy. No vector database was added. Stable catalog text is cached per
catalog; business memory is fetched once per request, and provider clients are shared across runs while cost
trackers remain private. SQL results remain bounded and keyed by the dataset, semantic files and prompts. Definition caches refresh on
version changes. Old paused runs without the current version fingerprint must be asked again.

`pantree-profile --publish-joins` generates reusable metadata during data upload. A missing artifact safely
falls back to measurement. KPI pulse runs outside web startup. The web queue, retained completed histories and
in-memory terminal checkpoints are bounded/cleaned. Quota increments and email-code attempts are atomic.
Chat history and its last successful QuerySpec use one bounded database read. PostgreSQL checkpoints use a
pool capped at four connections. Terminal status, chat answer and spend commit together once. Ask requests accept
an optional `idempotency_key` (8–128 letters/digits/underscores/hyphens), scoped to the signed-in user and chat;
retrying returns the same run ID, and a changed payload returns 409. This does not resume interrupted model calls.
The app still uses one instance; active job leases and distributed event streaming are not implemented.

## Validation

`tests/reference_sql.py` and the gold SQL suites independently check the compiler. New adversarial fixtures
exercise duplicate facts, NULL and absent groups, date boundaries, numerator/denominator behavior, concurrency,
and grounding. `evals/fastpath_baseline.py` checks the five reviewed question examples without a model.
`evals/context_retrieval.py` measures offline metric recall only. Paid evaluation defaults to `--mode autonomous`: blind scripted confirmation of proposed plans, without gold-guided corrections. Actual unresolved
clarification remains incomplete. Confirmation compliance is reported separately from interpretation accuracy.
`--mode assisted` is an explicitly separate run. Reports refuse mixed modes and separate intended plans, gold
result matches and checked evidence from unmeasured human narrative faithfulness. `evals/post38_benchmark.py`
measures local startup, warm queries, PostgreSQL history/checkpoints and Parquet versus materialized DuckDB.

See [PR #40 validation](pr40-validation.md) for current evidence and pending external measurements.


## Conversation protocol

`POST /api/chats/{id}/ask` accepts both new questions and replies. A reply carries `pending_run_id`,
`plan_version`, a natural-language `question`, and an optional unique `idempotency_key`. The existing
`/api/runs/{id}/answer` endpoint accepts `message`, `plan_version`, and that same retry key. A stale version
returns 409. Only a positive full-message confirmation approves execution; corrections, blanks and unresolved
questions do not. “Cancel” ends a pending plan without running it. Legacy Python `use_defaults` arguments are
ignored; the UI and CLI have no automatic approval controls.

The plan stores compiled, safety-checked SQL with its exact specs/templates before approval. After approval,
execution checks its digest and data snapshot, streams each verified table, then grounds each explanation and
builds its chart. The result panel switches between every approved query, including its exports. Changes to a
multi-query plan explicitly target a numbered step, preserving other steps; no dynamic query expansion runs
without a fresh proposal. Revision patches use the same interpreter and complete semantic catalog.

Proposals and replies are durable messages. One admission lock serializes web replies; the database also allows
only one paused-to-running transition. Duplicate reply keys replay events, including after process state is
lost. Running work interrupted by a process failure is marked interrupted rather than automatically rerun.
Paused PostgreSQL checkpoints can resume; in-memory checkpoints cannot survive a process restart.
