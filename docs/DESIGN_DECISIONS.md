# Design decisions

## 1. Python-generated SQL

**Decision:** The model returns a QuerySpec or a reviewed template call. Python compiles SQL.
**Alternatives:** Let the model write SQL, then validate or retry it.
**Why:** Business definitions, allowed joins, and confirmation can be checked before a query runs.
**Trade-off:** Novel questions need new governed capabilities. This does not remove interpretation errors.

## 2. YAML semantic layer

**Decision:** Keep named metrics, dimensions, relationships, and ambiguous phrases in versioned YAML.
**Alternatives:** Put definitions only in prompts or duplicate them in dashboards and application code.
**Why:** Reviewers can trace a number to one definition and test changes against reference SQL.
**Trade-off:** Someone must maintain definitions and dataset metadata. YAML correctness still requires tests.

## 3. Mandatory plan confirmation

**Decision:** Show the calculation, scope, and dates before executing it.
**Alternatives:** Auto-run every plausible plan or ask clarification only when model confidence is low.
**Why:** Users can correct a business assumption. Approval binds the exact plan version, SQL, and snapshot.
**Trade-off:** Confirmation adds an interaction. Blind approval cannot prove the plan matches the user's intent.

## 4. Numeric grounding

**Decision:** Explanations cite result cells and operations; accepted values are recomputed before display.
**Alternatives:** Ask the model to summarize a table freely or use a second model to judge its prose.
**Why:** Numeric checks are repeatable and identify unsupported values without another paid judge.
**Trade-off:** Qualitative claims and unfamiliar causal wording can still escape lexical checks. Human narrative review is separate.

## 5. Explicit handling of join inflation

**Decision:** Combine fact tables after aggregation; use correlated EXISTS for membership-only filters.
**Alternatives:** Join raw fact tables and aggregate afterward, or rely on DISTINCT to hide repeated rows.
**Why:** One order matching several events can otherwise be counted several times. EXISTS checks whether a match exists without copying the order.
**Trade-off:** Unsupported projections across one-to-many joins are rejected. Distinct counts and ratios need their own denominators.

## 6. Full catalog rather than a vector database

**Decision:** Retain the full governed catalog on the model path; use lexical retrieval for older chat evidence.
**Alternatives:** Retrieve only a few definitions using keywords, BM25, embeddings, or a hybrid search.
**Why:** The fresh [metric-recall comparison](../evals/reports/current/context-retrieval.json), produced by [context_retrieval.py](../evals/context_retrieval.py), shows that top-k lexical selection can omit required definitions.
**Trade-off:** A full catalog costs tokens and will become expensive as it grows. This comparison does not test vector retrieval and cannot establish that a vector database would be worse.

## 7. One Cloud Run instance

**Decision:** Keep active jobs, admission locks, and event streams in one process; persist chat data and paused checkpoints in PostgreSQL.
**Alternatives:** Distributed workers with durable job leases and a shared event stream.
**Why:** The deployment is simpler and avoids pretending an in-process lock protects multiple instances.
**Trade-off:** A process restart interrupts active work, and horizontal scaling needs additional coordination. No 100-concurrent-user claim is made.

## 8. Autonomous evaluation with a fixed budget

**Decision:** Select cases before inference; blindly confirm plans, score against hidden reference SQL, and reserve the maximum configured call cost before sending it.
**Alternatives:** Use gold-guided clarification, hand-pick successful examples, or check spend only after calls finish.
**Why:** This exposes unresolved scope and wrong interpretations while keeping the run's cumulative reservation within its ceiling.
**Trade-off:** Real users can resolve ambiguities that this autonomous test cannot. The budget may stop the run before all 100 cases; pricing accuracy depends on configured rates.
