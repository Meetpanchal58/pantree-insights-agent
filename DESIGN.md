# Design decisions

Each entry: the decision, the alternatives considered, why this one, and its known weakness. Numbers quoted
here come from reports in `evals/reports/`.

## 1. Safety is enforced in code, not by the model

**Decision.** Every query passes `guardrails.check_sql` (sqlglot) before it reaches DuckDB: one read-only
SELECT, approved tables and columns (resolved with `qualify()` against `semantic/schema.yaml`), approved join
paths only (ON, WHERE and USING), no table functions, no `SELECT *` or whole-row arguments, results summarised
or limited. The DuckDB connection itself is locked (`enable_external_access=false`, allowed directories,
`lock_configuration`), and blocked tables and columns do not exist in its views.

**Alternatives.** Ask the planner or validator to refuse unsafe SQL; a database role with grants only.

**Why.** A model can be talked out of a rule; a parser cannot. Two independent layers (parser + locked
connection) mean a bug in one is not a leak.

**Known weakness.** The checks are syntactic. A query that is allowed but answers the wrong question is only
caught by the validator, which is the same model family as the writer. The regex question check in front is a
cheap filter only: on `evals/behaviour.yaml` it blocks none of the 11 adversarial questions (the planner and
the SQL guardrails must catch them), in exchange for 0 of 16 false declines.

## 2. The SQL writer first sees only the tables the plan selected

**Decision.** Retrieval returns up to 6 tables (`MAX_TABLES`) so a needed table is rarely missing; the plan
picks its `source_tables` and `join_path`, and the writer's first attempt is narrowed to those. Retries get
the full retrieved context.

**Alternatives.** Give the writer everything retrieved; retrieve fewer tables.

**Why.** Retrieval recall matters more than precision (holdout: 29/31 questions get all required context;
table precision is 27% by design), but extra tables distract the writer into wrong joins. Narrowing keeps
recall at retrieval time and precision at writing time.

**Known weakness.** If the planner forgets a table, the first attempt fails and costs a retry. The ablation
variant `planner` measures this (see the README for whether it was run).

## 3. Numbers in answers are structured claims, checked against the result

**Decision.** The analyst returns each number as a claim (`grounding.Claim`: value, unit, column, row keys,
operation such as diff, ratio, share, pct_change). `grounding.verify` recomputes each claim from the result
table (0.5% relative tolerance, or equal after display rounding) and the text uses placeholders that are
rendered from the claims. Any other number in the prose fails, except dates, years and small integers.
One rewrite is allowed; claims that still fail are shown as a warning and the answer is not saved to memory.

**Alternatives.** The previous regex check (does this number appear in the result, or a simple derivation
of it?); an LLM judge.

**Why.** The regex check accepted 100% of random percentages, 83% of random ratios and 32% of random counts
on an 8-row table, so its "grounded" rate measured little. Structured claims accept 0.10-0.45%
(`evals/reports/grounding_2026-11-15_1318.md`). An LLM judge would add cost and share the analyst's blind spots.

**Known weakness.** Claims prove a number is in the result, not that the result answers the question;
that is the SQL path's job. Some legitimate derived numbers (e.g. multi-step calculations) cannot be
expressed as one claim and must be left out of the text.

## 4. Results grouped by an identifier must be a ranked top-N

**Decision.** `identifier_grain_errors`: if the outer SELECT groups by an identifier column (customer_id,
order_id, session_id, ... listed in `schema.yaml`), it must have ORDER BY and a LIMIT of at most 500.

**Alternatives.** Block identifier grouping entirely; allow it with only the global row limit.

**Why.** "Top 20 customers by spend" is a fair business question; "spend per customer" for 100,000
customers is a row-level dump by another name. The rule allows the first and turns the second into an
error message that tells the writer how to fix it.

**Known weakness.** The list of identifier columns is maintained by hand; a new key column not added to it
is not covered.

## 5. Durable paused runs while keeping the single-instance serving model

**Decision.** Local tests use `InMemorySaver`. The production setting
`CHECKPOINT_BACKEND=postgres` uses a LangGraph PostgreSQL checkpointer in Neon so that
paused clarification checkpoints can be restored after a Cloud Run restart. Run ownership and
status are stored in SQLAlchemy tables, and runtime checkpoint memory is reattached per user.

**Alternatives.** Stateless full replay (unsafe because an already-paid or running LLM call
could execute twice), an in-memory-only demo, or horizontally scaled shared event delivery.

**Why.** Paused user interactions deserve durability without a second infrastructure service.
The application retains `max-instances 1`; active SSE events still live inside that instance.

**Known weakness.** An active model call interrupted mid-flight is marked for recovery and
not automatically replayed. Multiple Cloud Run instances require shared SSE/event state,
a distributed run lock, and a database-backed claim of execution ownership. These are
not implemented; do not describe this as horizontally scalable.

## 6. Memory is written only for verified answers, and old insights age out

**Decision.** An answer is saved as an insight only if its claims verified, no reviewer concern was raised,
and memory is enabled for the run. When reading, insights older than `memory_max_age_days` (30) relative to
the data's last day are left out.

**Alternatives.** Save every answer; let the model decide what to keep.

**Why.** Memory feeds the planner. A wrong answer saved once would be repeated as "known" in later
questions, so the bar for writing is the same check the answer itself must pass. Ageing out stops last
quarter's numbers being quoted as current.

**Known weakness.** An answer can be grounded and still wrong (correct numbers from a wrong query), and
such an answer is remembered. The ablation `full_no_memory` measures memory's effect (see the README for
whether it was run).

## 7. Pulse uses robust z-scores with a false-discovery-rate control

**Decision.** Each KPI series compares the latest week with the 8 before it using a robust z-score
(median and MAD) and selects alerts with Benjamini-Hochberg at q = 0.10 across all series. Changes explained
by sale events are labelled, not raised. The old mean/SD rule (z >= 2.5) remains as `method: legacy`.

**Alternatives.** Mean/SD z-scores with a fixed threshold; a forecasting model per series.

**Why.** With 143 series, a fixed threshold yields false alerts in proportion to the number of series; BH
controls the share of false alerts among those raised, and median/MAD is not dragged by the previous sale
week.

**Known weakness.** On a backtest with 150 injected shocks (`evals/reports/pulse_backtest_2026-11-15_1303.md`),
robust_bh found more shocks (43/150 vs 35/150) but raised more other alerts (2.77 vs 1.17 a week). Neither
method is good at small changes (+25%: 5/26 vs 3/26). Which default to keep is an open decision.

## 8. Understand the question before planning, and ask until it is clear

**Decision.** A dedicated understanding step reads the question (with every answer so far) into a frame:
subjects, measures, mentions, cuts, period and open points. Code then resolves the words to columns with the
concept map, the value index and measured ID formats, and adds open points the model missed. Anything unclear
goes back to the user, round after round, with no fixed number of questions; the planner no longer asks.

**Alternatives.** The planner asks at most 3 questions once (the previous design); never ask, always assume.

**Why.** The same word means different columns for different subjects ("Mumbai" is a delivery city for orders,
the customer's city for sign-ups, and also a seller city, a warehouse and a brand). Deciding the subject is
easier for a model than choosing columns, and the rest can be done in code and checked by tests.

**Known weakness.** Each question costs one more LLM call, and one per extra round. A model that keeps asking is
stopped after 6 rounds (defaults are used and a warning is shown). Whether the model asks the right questions,
and not too many, has not yet been measured end to end (the over-asking suite will show it).

## 9. Join paths and join rules in code, from measured cardinality

**Decision.** The data profile measures every approved join (1:1, N:1, 1:N, N:N and unmatched rows). Join paths
from the question's table are built in code (cheapest route; joins that repeat rows cost more; ties go to the
root table's own join), and joins that need an extra condition carry it as a join rule the guardrail enforces.

**Alternatives.** Let the SQL writer pick joins from the approved list (the previous design).

**Why.** Join mistakes were the main source of wrong totals, and some are invisible without measuring: the
coupon-code join to promotions looked fine but matched several promotions per line (+7% coupon revenue).

**Known weakness.** The profile is measured once per data snapshot (about 5 s at start-up); a join whose
cardinality changes in later data is only re-checked when the snapshot changes.
