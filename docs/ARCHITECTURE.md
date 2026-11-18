# Architecture

## The guarantee

> Every number the user is shown is correct for the confirmed question, the governed definitions and the data
> snapshot. When the system cannot reach that bar it asks, or says it cannot answer as a verified number yet. It
> never guesses.

A question ends in one of three ways only: a **verified answer**, a **question back to the user**, or **"I can't
answer this as a verified number yet"** (with what it can answer). There is no path for a plausible wrong number.

The design follows *structural abstention* ([Never the Number, 2026](https://arxiv.org/html/2608.13926)): a
language model may decide **which question** is answered, never **which value** is returned. The model reads and
writes language; code builds every query.

## Flow (LangGraph, `src/pantree_insights/graph.py`)

```
check_input     code: personal data, injection, length                         -> decline
interpret       interpret.py:
                  ambiguity register (glossary.yaml `ambiguous`)               -> ask
                  interpreter: question -> QuerySpec | template | clarify | unanswerable | decline
                  validate against the catalog, normalise values, one repair   -> ask if still invalid
                  contrast: alternative readings compiled and RUN; numbers differ -> ask
                  premise: a period outside the data is stated
clarify         pause (LangGraph interrupt): questions, then the read-back built from the spec; nothing runs
                until the user confirms; a correction goes back to interpret
execute         spec.py compiles the spec, templates.py builds a reviewed analysis; same safety checks as ever;
                cached by (SQL, data snapshot)                                  -> trust: verified
analyse         analyst writes text with {c1} placeholders; code fills numbers from the result; a sentence whose
                number cannot be checked is removed
chart, remember  chart; verified insight saved to memory
```

## Layers

| Layer | Files | Role |
|---|---|---|
| Knowledge | `semantic/schema.yaml`, `metrics.yaml`, `concepts.yaml`, `glossary.yaml`, `catalog.yaml` (generated) | what each table, column, value and metric means; splits per subject; business terms; two-meaning phrases |
| Request | `spec.py` | the QuerySpec (metrics or catalog aggregates, splits, filters, period and grain, ranking, share of total, row lists); validation; deterministic compiler from metrics + concept map + measured join graph (`joins.py`, `profile.py`) |
| Analyses | `templates.py` | reviewed SQL for multi-step questions: event lift, ROAS from own attribution, platform vs recorded orders, repeat purchase within N days |
| Understanding | `interpret.py`, `prompts/agents.yaml` | interpreter and contrast calls, ambiguity register, premise checks, read-back |
| Orchestration | `graph.py` | the flow above, pause/resume, cache, trust label |
| Explanation | `agents.py`, `grounding.py`, `charts.py` | analyst output, claim verification, charts |
| Safety | `guardrails.py`, `db.py` | input guard; read-only DuckDB views without blocked columns; SQL checks on every query |
| App | `web.py`, `auth.py`, `store.py`, `memory.py`, `pulse.py`, `demos.py`, `static/` | FastAPI app, sign-in, chats and projects, memory, KPI pulse, offline demos |

## What can go wrong, and the control

| Layer | Failure | Control |
|---|---|---|
| Understanding | a phrase with two meanings read one way ("wrong size") | ambiguity register: always asked |
| | a confident misreading | contrast: alternative readings are run; different numbers -> the user chooses; the read-back states exact definitions and must be confirmed |
| | an unknown term, value or column | validation fails -> one repair -> asked |
| | a question no spec or template expresses | "can't answer as a verified number yet" (no free-form SQL) |
| | a false premise (no data in the period) | stated in the read-back and the answer, never a silent zero |
| Query | SQL differing from a definition | compiled from metrics.yaml; tested on every gold answer and every metric x dimension against an independent implementation (`tests/reference_sql.py`) |
| | a join that repeats rows | refused using measured cardinality |
| | names several IDs share (Payday Sale x 12) | grouped with their ID |
| Explanation | a number typed by the model | numbers are claims rendered by code; an unverifiable sentence is removed |
| Knowledge | a wrong or one-sided entry | knowledge-base lint (`tests/test_knowledge_base.py`): an alias maps to one value; a two-meaning phrase is never an alias |

## Why the model never writes SQL

Measured on this project: with the question already resolved, model-written SQL was still wrong on some questions
(a missing filter, a missing share), and retries turned correct queries into wrong ones. Compiled SQL is the same
every time, is tested exhaustively, and costs nothing. Industry results point the same way: semantic-layer
compilation 98-100% vs 84-90% for text-to-SQL on the same data, and outside its scope it errors instead of guessing
([dbt 2026](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026)); model choice matters far less than
supplied business semantics ([2026 benchmark](https://arxiv.org/abs/2604.25149)).

## Testing the user loop

Evaluations run each answer question with a **simulated stakeholder** (`evals/run_eval.py`) who knows the intended
spec (`evals/gold_specs.yaml`): it answers questions as the asker would and confirms a read-back only if it computes
the intended thing, otherwise it states what it wants. It is never deployed. Release gate: every verified answer
equal to the gold, zero silent wrong answers. Free checks: `evals/spec_eval.py` (every gold spec or template returns
the gold answer) and the test suite.

## Speed, load and cost

- 3 model calls per answer (interpreter, contrast, analyst); the catalog prompt prefix is identical for every
  question, so the provider caches it. Measured about US$0.01 per answer on the Nov 18 smoke test.
- Results cached by (SQL, data snapshot): a repeat question is exact and needs no query.
- Web runner: at most `MAX_CONCURRENT_RUNS` questions at once per instance; others queue with a visible position;
  provider calls are bounded by `MAX_PARALLEL_LLM_CALLS` with retry on rate limits.

## Research basis (Nov 2026)

| Finding | Adopted as |
|---|---|
| structural abstention: generative shell, deterministic kernel, confirmation ([2026](https://arxiv.org/html/2608.13926)) | the whole flow |
| disagreement between deliberately different readings finds material ambiguity ([DisGuAD](https://arxiv.org/html/2611.21645), [AIR](https://arxiv.org/html/2610.24248)) | contrast check with execution |
| ambiguity taxonomy, multiple-choice clarification ([AmbiSQL](https://www.arxiv.org/pdf/2508.15276)) | ambiguity register |
| reliability = correct answers + correct abstention ([TrustSQL](https://arxiv.org/pdf/2403.15879), [PremiseSQL](https://arxiv.org/pdf/2610.22158)) | unanswerable route, premise checks, silent-wrong gate |
| verified question-query repositories ([Snowflake](https://docs.snowflake.com/user-guide/snowflake-cortex/cortex-analyst/verified-query-repository)) | templates; reviewed specs as the next step |
