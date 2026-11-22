# Phase 1 validation

Scope: reorganize code without changing analytical behavior, then fix test collection without a downloaded dataset. Documentation redesign and paid evaluations belong to later phases.

| Check | Result |
| --- | --- |
| Full pytest suite, small data and PostgreSQL 17 | 1,708 passed; 1 existing snapshot-dependent skip |
| Collected tests | 1,709 |
| Fresh local clone, no downloaded data, PostgreSQL enabled, pytest -m "not data" | 545 passed; 1,164 deselected |
| Missing snapshot, compiler tests without deselection | 900 skipped with download instructions; no collection error |
| Ruff and git diff --check | Passed |
| tests/frontend.cjs | Passed |
| tests/workspace.cjs | Passed |
| Wheel build and package contents | Passed; all split Python and browser modules included |
| pantree-ask --help | Passed with the unchanged CLI entry point |
| Source size | Largest source file: styles.css, 334 lines; no source file over 400 lines |

The Python function comparison covered 217 functions, including named nested functions. Imports were allowed to move, and registered routes now explicitly read the app-owned web.runner. App construction delegates registration to the route modules. The tests check these wiring changes. Browser function bodies were moved to ES modules; the tests now import their exports rather than relying on script globals.

No paid provider call or model evaluation was run. These results establish regression coverage for the reorganization; they do not establish improved model accuracy or production performance. See [the function map](phase1-moves.md) and [refactor notes](../REFACTOR_NOTES.md).
