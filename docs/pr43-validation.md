# Adaptive investigations and durable conversational retrieval

Baseline: merged PR #42 (`d20378d`), 22 November 2026. This change makes investigations adapt to observed
results and retrieves relevant older analyses from durable records. It does not create unlimited model
context or establish causal identification from observational data.

## Investigation behavior

The interpreter enables investigation mode for requests to investigate changes, root causes or competing
explanations. The first read-back explains that further checks may follow and need separate approval.
Ordinary reporting and drill-downs keep their existing flow.

After an approved query batch executes and its explanations pass grounding, the investigator receives
bounded result excerpts, the objective, governed catalogue and prior probe ledger. It may stop or propose
up to two typed QuerySpecs. Each proposal names an unproven hypothesis, a competing explanation and exact
query/row/column evidence references. Code resolves the cited values, validates and compiles the query,
retains the initial filters and reporting period, and presents a new versioned confirmation.

Only that confirmation authorizes execution. New batches cannot bypass the existing SQL guard, approved
joins, native-grain metric aggregation, snapshot checks, pinned provider configuration or spend reservations.
A narrowed population is allowed; conflicting scope and non-conformed cross-table filters stop the probe.
Period comparison operations can investigate the earlier period while preserving the original current
reporting window. A user can explicitly revise a proposed scope before confirming it.

The checkpoint preserves completed results, analyses, hypotheses, exact citations and round count. Results
remain available if a subsequent proposal fails or is cancelled. Successful completion includes a deterministic
overview referencing each query's verified headline and individual charts/tables. This is evidence organization,
not a new unverified causal synthesis. Hypotheses remain explicitly unproven.

Stopping rules:

- At most three executed rounds and eight queries per request, including the initial batch.
- At most two proposed probes per review; the initial approved batch may contain four queries.
- No further review after twelve recorded model calls or USD 0.25 recorded spend. These are pre-review
  thresholds, not a guaranteed per-request price ceiling: a call already in progress or an approved batch's
  explanation may cross a threshold. Existing account/provider reservations remain the authoritative spend
  enforcement. The additional review uses the same pinned model and can consume allowance.
- Stop for incomplete/unverified evidence, invalid citations, unsupported plans, exact repeated SQL, exhausted
  budgets, or no supported next probe. Semantic paraphrases of the same query may evade exact SQL equality;
  round/query bounds still terminate the loop.

A stop explains its practical reason. Completed evidence is retained; the user can start another request to
continue. The workflow does not claim a cause was proven just because a hypothesis was investigated.

## Durable context

The model still receives a bounded recent conversation window. In addition, `Store.recall` searches completed
run questions across the entire current chat's persisted history, ranks lexical matches in SQL and fetches
at most three full records. Explicit run IDs have priority. There is no last-N-chat-turn cutoff on this search.
Only the matching plans, verified finding, timestamps, snapshot state and hypothesis ledger enter the prompt;
full population membership stays server-side.

Historical references use `result_scope.run_id`, query index and field. Latest-result references omit run ID;
an older matching population cannot silently replace the latest one. Code enforces user AND chat ownership.
Stale historical findings are marked for recomputation, and their populations cannot be bound to current SQL.
Current explicit instructions/corrections take precedence over historical evidence. Sources are visible in
the result panel so users can identify the retrieved analyses.

Existing PR #42 runs already persist final events and populations; no migration, new database, vector service
or extra provider call is required. Records before exact populations were introduced can provide historical
background but cannot establish an exact old result set. The existing eight-summary active investigation
record remains an efficient working context, rather than the only available history.

This is lexical retrieval, not perfect semantic recall: an unrelated synonym or ambiguous “the very first
analysis” may not identify the intended run. Naming its topic or using the displayed run ID gives a precise
reference. At most three relevant old analyses enter one request; historical storage is durable, model context
is still bounded. General persistent user preferences continue to use the existing preferences/project memory.

## Validation

Regression coverage includes:

- Evidence-dependent next queries, competing hypotheses, fresh approval and stale approval rejection.
- Invalid query/row/column citations, repeated probes, round and spend gates, scope preservation and cancellation.
- Individual charts and result populations across rounds; historical references never replace latest references.
- Finding an older analysis beyond 35 newer requests, restart durability, stale snapshots and cross-user/chat denial.
- Real PostgreSQL checkpoint restart between approved rounds, plus PostgreSQL historical retrieval.
- Existing SQL/compiler, grounding, application and browser suites.

Tests use real simulator data and scripted models, without paid inference. Final local and GitHub CI outcomes
are recorded in the pull request. No production deployment or hosting setting change is included. A live-model
benchmark is still needed to measure whether the selected hypotheses and next queries are useful on unfamiliar
business questions; passing execution tests does not establish that capability by itself.
