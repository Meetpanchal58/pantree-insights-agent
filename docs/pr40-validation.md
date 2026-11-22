# PR #40: conversational analyst validation

Baseline: PR #39 merged at `04d6b8019572f3856ac915d273935dd451794958`. Implementation commit:
`844f63dd2137a00c4ed3aff6ef3199283000f195`. Measurements use the local small simulator snapshot
(seed 42, 2025-01-01 through 2026-11-22). No production deployment or paid model requests were made.

## Implemented behavior

- Every analytical request, including deterministic grammar routes, pauses for a plain-language plan.
  The normal composer accepts corrections, confirmations and cancellation. Questionnaire controls, recommended
  execution and CLI defaults are removed. Proposals and replies persist as ordinary chat messages.
- Approval binds a plan version, snapshot and digest of the specs/templates and compiled safe SQL. The execution
  boundary rejects absent or changed approval. Compilation happens before approval; execution uses those exact
  statements. Blank, stale, ambiguous and correction-bearing replies cannot approve a pending plan.
- Structured patches preserve fields the user did not change. Reviewed dimension/date follow-ups remain
  deterministic. Ambiguous multi-query corrections ask which numbered step should change and preserve others.
- The interpreter can propose up to four dynamically chosen governed queries under one approval. Tables stream
  individually; every query has its own grounded explanation, chart, SQL and exports. No unapproved expansion or
  autonomous investigation loop runs. Subsequent questions can use prior findings to propose further work.
- A per-chat durable context retains the objective, origin/current plans, current metric definitions and eight
  verified evidence summaries. Each result excerpt is marked with its full row count and whether it is excerpted.
  Complete plans, SQL and results remain in saved runs. Independent objectives reset the working context;
  refinements, drill-downs, comparisons and continued investigations retain it. Owner checks prevent cross-user reads.
- Coverage checks include comparison periods and rolling lookbacks; unequal-duration comparison totals require
  clarification. Missing comparison groups remain unknown. Non-additive shares are rejected. Additive period
  comparisons can report contributions to net change; unknown components make contributions unknown.
- Cohorts use the recorded first-order flag. Segmentation accepts governed population filters; ordered funnels
  accept entry filters and device/source splits. Unsupported template parameters and explicit scope omissions
  are rejected. Filters on product lines disclose their order and revenue populations in the read-back.
- Claim checks bind known entity/period labels to evidence rows, including two values swapped within one sentence.
  Findings, unproven hypotheses and recommendations have separate fields. Existing numerical, unit, direction,
  rank, causality, truncation and deterministic-fallback checks remain.
- Initial requests and replies support idempotency. A database transition accepts a paused reply only once;
  API retries replay existing events, including after in-process state is discarded. Reconnect starts at the
  latest conversation boundary. Paused PostgreSQL plans resume after restart; interrupted active work is not
  automatically rerun. Cancellation performs no analytical query.

## Correctness evidence

| Check | Baseline | Candidate |
|---|---:|---:|
| Local full pytest | 1,565 passed; 5 skipped | 1,601 passed; 1 skipped |
| Subsequent affected-area tests (final entity binding and dynamic-plan cases) | — | 87 passed |
| Conversation suite included in affected-area run | — | 19 passed |
| Evaluation gold SQL validation | 129 valid | 129 valid, zero problems |
| Compiler answers, 98 cases × 3 passes | 294/294 | 294/294 |
| Independent no-model benchmark answers, 5 cases × 5 passes | 25/25 | 25/25 |
| Browser regression | prior suite | passes, including desktop/mobile replies and multiple results |
| Ruff / whitespace checks | passes | passes |

The baseline full run did not have a PostgreSQL DSN, so four integration tests were skipped. Candidate full
validation includes six real PostgreSQL tests: quota/spend concurrency, terminal idempotency, transaction rollback,
checkpoint restart, concurrent replies and durable context/isolation beyond the recent-history window. The one
remaining skip is the pre-existing demo/snapshot availability check. Final new cases were rerun with all affected
agent/grounding tests; CI runs the complete final tree.

The 19 conversation tests exercise the five supported confirmation phrases, five empty/missing/stale input
variants, revision preservation/version invalidation, direct execution tampering, cancellation, batch execution
and duplicate rejection, targeted batch revision, structured patch preservation, durable context ingestion,
zero-model contextual follow-up and a scripted dynamic investigation. They are regression evidence, not an
estimate of arbitrary natural-language interpretation accuracy.

Autonomous evaluation now blindly confirms proposals with “Go ahead”; the gold spec never supplies corrections.
It separately records initial plan correctness, confirmation compliance, confirmations, final SQL correctness
and answer correctness. A mandatory approval pause is plan-ready, not an interpretation failure. Genuine
unresolved clarification remains incomplete. Assisted and compiler-only evaluations remain separate. Existing
gold SQL and expected analytical answers were not changed.

## Processing latency (seconds, P50 / P95)

The same five independently checked questions ran five times against each source tree. Warm samples are the
last four passes (n=20); first-pass samples are n=5. Initialization is n=3 fresh Agent objects, with Python imports
and OS caches potentially warm. All calls use a forbidden-model stub. These are local microbenchmarks, not
production latency, controlled Cloud Run cold starts, or representative model-based investigations.

| Measurement | Baseline | Candidate |
|---|---:|---:|
| Agent initialization, n=3 | 0.0939 / 0.5463 | 0.0532 / 0.5505 |
| First-pass total processing, n=5 | 0.0922 / 0.2560 | 0.0936 / 0.2292 |
| Warm total processing, n=20 | 0.0519 / 0.0628 | 0.0593 / 0.0722 |
| Interpretation/proposal, n=25 | inseparable: baseline auto-executed | 0.0208 / 0.0279 |
| Post-confirmation processing, n=25 | no confirmation phase | 0.0396 / 0.1231 |
| Conversational revision, n=5 | not measured with equivalent protocol | 0.0230 / 0.0298 |

Each candidate phase is timed around an active start/resume call; stakeholder waiting is excluded. Total is the
sum of active phases. The gate adds a small local cost, rather than demonstrating a general speedup. Each run
made zero model calls, zero model tokens and $0 model spend. Full-model inference, provider queueing, prompt
cache behavior and cost remain unmeasured. Per-stage timings are included in the raw benchmark records.

Local resource experiments used a 25,796-row sales fact and a fixed monthly aggregation (20 repetitions per
configuration). Local Parquet P50 was 0.00377 s; a materialized three-column DuckDB copy was 0.00232 s, with
0.04273 s setup cost. DuckDB thread counts 1/4 and memory limits 128/512 MB gave P50 values between 0.00335 and
0.00386 s. That small workload does not justify larger production allocations or persistent materialization.

The local PostgreSQL benchmark used 2,000 chat messages. Bounded history P50 was 0.00256 s. Checkpoint write/read
P50 was 0.00152/0.00216 s. Forty history reads took 0.600 s without pooling versus 0.143 s with pooling at one
worker, and 0.471 versus 0.078 s at four workers. Per-request tails increased under contention. Existing pooling
is retained; no new infrastructure or deployment configuration was introduced. These local numbers do not
predict managed PostgreSQL network latency or Cloud Run concurrency.

## Size and simplification

| Runtime measure | Baseline | Candidate |
|---|---:|---:|
| Python modules | 24 | 24 |
| Python lines | 6,245 | 6,746 |
| Python bytes | 316,464 | 351,430 |
| Python + JS/CSS/HTML lines | 7,522 | 7,995 |
| Python + JS/CSS/HTML bytes | 436,129 | 468,974 |
| Direct runtime dependencies | 20 | 20 |

Counts include all tracked runtime source, including the existing generated offline-demo JavaScript, and exclude
semantic assets, tests/evals and documentation. Net growth implements the requested workflow in existing modules;
no runtime module, framework, agent, dependency or distributed service was added. Removed questionnaire rendering,
obsolete CSS, automatic approval state and CLI defaults. Reused existing interrupts, compiler primitives, analyst,
charts and store transactions. Exact approved SQL avoids recompilation after confirmation. Reviewed explicit
follow-ups do not load durable evidence into an unnecessary model call; contextual investigations still receive it.
Contrast is skipped for independently matched grammar or explicit low-risk scopes, retained for material ambiguity.

## Limits and pending measurements

- No live-model credentials or deployed-infrastructure credentials were available. General first-turn accuracy,
  complex autonomous investigation accuracy, narrative faithfulness, answer coverage, silent-wrong rate and
  live end-to-end correctness are **not measured**. The requested ≥95–99% targets are not claimed as met from
  compiler tests or scripted models. Cloud Run connection/cold starts, GCS mounts and paid-model latency/cost
  also remain pending.
- Intent and entity checks are conservative lexical safeguards around typed plans and explicit approval. They
  do not prove arbitrary language equivalence, prevent every invented entity, or establish causality. Revisions
  requiring a change of analytical method ask for a complete new proposal. Some combinations remain unsupported.
- The working evidence context is bounded to eight summaries with six-row excerpts. Older complete evidence is
  preserved in chat history, but automatic retrieval of an arbitrary old result is not implemented. Investigations
  that need omitted rows must propose a fresh governed query; summaries must not be treated as full populations.
- Batch explanations are grounded per query; there is no independently verified causal synthesis engine.
  Contribution to net change is descriptive, can be negative or exceed one, and is undefined when net change is zero.
- Only supported paused PostgreSQL checkpoints survive restarts. Active calls interrupted by a crash require
  a new request; this deliberately avoids automatic duplicate execution. The application remains single-instance.

## Reproduction and artifacts

```bash
uv run ruff check .
PANTREE_TEST_POSTGRES_DSN=postgresql://... uv run pytest -q
PLAYWRIGHT_BROWSERS_PATH=/path/to/browsers node tests/frontend.cjs
DATA_SIZE=small uv run python evals/run_eval.py --suite all --dry-run
DATA_SIZE=small uv run python evals/baseline.py --repeats 3
DATA_SIZE=small uv run python evals/conversation_benchmark.py
DATA_SIZE=small PANTREE_TEST_POSTGRES_DSN=postgresql://... uv run python evals/post38_benchmark.py
```

For the before measurement, run `conversation_benchmark.py` with `PYTHONPATH` pointing to a checkout of the exact
baseline's `src` directory and the same small data. The helper supports both protocols; baseline phase fields are
null because it executes without a pause. Raw evidence includes source hashes and snapshot metadata:
[before](validation/pr40-before.json), [after](validation/pr40-after.json),
[storage/resources/PostgreSQL](validation/pr40-storage-postgres.json),
[compiler cases](validation/pr40-compiler.json), [size/dependencies](validation/pr40-size.json).
