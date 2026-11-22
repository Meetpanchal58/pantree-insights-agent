# Phase 1 refactor notes

This phase changes code organisation and test setup, not analytical behavior.

- Graph node methods are grouped by interpretation, execution, and delivery. Agent retains its existing public methods through small internal mixins (classes that supply methods).
- Store uses the same SQLAlchemy tables, statements, and transactions. Its methods are grouped by accounts, chats, runs, quotas, and memory. No database migration is needed.
- Web routes are registered in focused modules. The package still owns the runner so test injection and the existing CLI work. Authentication stays in the existing auth.py router.
- Interpretation separates models, prompts, readings, readback, checks, revisions, and decisions. The compiler also needed a split to meet the source-size requirement.
- Browser modules use explicit imports and one module entry point. There is no bundler or new runtime dependency. Browser tests import the exports into their test page; production does not need test globals.
- Existing checkpoint initialization remains with Agent. Moving it into the application Store would mix separate persistence responsibilities.

All 217 existing Python functions were compared structurally against the base commit. Their signatures and bodies match after allowing import relocation and the explicit web.runner reference. App construction is the exception: it delegates route registration. The regression suites check that wiring.

Known pre-existing issue: test_spec.py opens the simulator catalog during module import. This prevents a clean no-data collection. The separate test-hygiene commit moves it into a fixture. The other specifically listed test modules already carry data markers on the current main branch.

No new production bug fix is included. Paid model evaluations are not run. Evaluation quality, latency, deployment topology, and model behavior are outside this phase.

## PR #46 evaluation observations (no production fix included)

The fresh autonomous evaluation exposes interpretation and evaluation-contract issues. Scores are not changed after seeing outcomes.

- Campaign last-click revenue (regression/h05) was interpreted as realized sales revenue. Those are different definitions and source populations. Numeric grounding did not establish intent correctness.
- The strict reference for regression/m06 includes shipped-line counts as well as the requested RTO rate. An answer containing a rate and order count fails that full-table contract; this alone does not establish that the displayed rate is wrong.
- The strict reference for regression/m08 includes inbound quantity. An out-of-stock answer omitting that numeric column fails the current scorer. Required versus optional reference columns need a separately reviewed rubric before another evaluation.
- Autonomous mode leaves business clarifications unanswered. Its raw questions counter records answered clarification turns; the new results builder derives actual asked questions from the trace and excludes confirmations.

The evaluation-only driver uses the production managed adapter's reservations plus a cumulative ledger. The older general CLI's after-spend check is left intact and documented; it is not used for the bounded fresh run.
