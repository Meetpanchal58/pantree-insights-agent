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
