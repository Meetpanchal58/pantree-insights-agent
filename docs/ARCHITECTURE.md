# Architecture

A model chooses an analytical request; governed Python code validates and compiles SQL. LangGraph retains
checkpointed clarification, deterministic execution, bounded repair, charting and memory. Verification describes
the selected scope and numeric evidence, not a proof of natural-language understanding or causality.

## Conditional flow

```text
input safety -> context (once per request) -> intent
  reviewed complete KPI grammar, no prior context -> validated QuerySpec
  otherwise -> interpreter -> structural + explicit intent checks -> at most one repair
    material ambiguity -> clarification checkpoint
    valid spec -> semantic contrast (no speculative SQL) -> read-back confirmation
    valid template -> parameter and supported-scope checks -> read-back confirmation
  -> single-source or grain-aware multi-fact compiler -> SQL safety -> execution
  -> snapshot check -> immediate result event
  -> deterministic KPI summary or analyst -> numeric/unit/prose checks -> at most one rewrite
  -> remove unsupported claims -> recompute accepted values -> chart -> scoped memory
```

A grammar match consumes the whole question. Unknown filters, modifiers, date forms, prior chat, answers or
business context use the interpreter. Relative dates follow the snapshot's last day and are disclosed. A known
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
Templates retain their reviewed SQL for event lift, channel ROAS, reconciliation and fixed-window repeat purchase.
Unknown template parameters and unsupported date scope cannot silently disappear.

## Evidence and delivery

The database result is streamed before explanation. Model claims name a result column, row keys, an operation
and a unit. The verifier checks those, rejects duplicate IDs, incomplete-population arithmetic, non-finite
values, small ungrounded numbers and common unsupported causal assertions. It does not sum or average ratios
without their underlying numerators and denominators. Accepted approximations are replaced by recomputed
values before display. ROAS is a multiple, rather than a percentage. Chart text and follow-up text are also checked.

These controls are not a complete theorem prover for prose. For example, arbitrary qualitative conclusions
and unrecognised causal paraphrases remain a limitation of the model explanation path. Fast-path summaries
use deterministic cell descriptions and make no causal claims. Model failures, unanswerable scope and material
ambiguity are separate outcomes; unsafe input and SQL remain blocked.

## Context and runtime

The full catalog is kept for the model path. An offline ablation of 66 named-metric cases found missing required
metrics in 13 cases with keyword top-five retrieval and five cases with BM25 top-five or top-ten retrieval.
This is metric recall, not model accuracy. No vector database was added. Stable catalog text is cached per
catalog; business memory is fetched once per request, and provider clients are shared across runs while cost
trackers remain private. SQL results remain bounded and keyed by snapshot.

`pantree-profile --publish-joins` generates reusable metadata during data upload. A missing artifact safely
falls back to measurement. KPI pulse runs outside web startup. The web queue, retained completed histories and
in-memory terminal checkpoints are bounded/cleaned. Quota increments and email-code attempts are atomic.
The app still uses one instance; active job leases and distributed event streaming are not implemented.

## Validation

`tests/reference_sql.py` and the gold SQL suites independently check the compiler. New adversarial fixtures
exercise duplicate facts, NULL and absent groups, date boundaries, numerator/denominator behavior, concurrency,
and grounding. `evals/fastpath_baseline.py` checks the five reviewed question examples without a model.
`evals/context_retrieval.py` measures offline metric recall only. Paid evaluation reports first-turn outcomes
before stakeholder assistance separately from later corrected outcomes.

See [validation and audit](refactoring-validation.md) for the tested snapshot, results and remaining work.
