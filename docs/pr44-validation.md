# Sent-message editing and confirmation recovery

Previously, sent user messages had no edit control or correction endpoint. A short typo at plan confirmation could enter the revision interpreter; a subsequent approval could repeat an unresolved clarification instead of presenting an actionable plan.

Every persisted user message now has an Edit control. The latest paused clarification reply can be corrected in place with an audit trail. Older messages and approvals that already executed a query create an edited conversation copy containing only preceding context; the original remains intact. Regenerated plans require fresh confirmation. Earlier verified evidence is retained with provenance, without copying spend or downstream answers.

Edits enforce ownership, expected prior content, current plan version, and retry idempotency. In-place corrections do not consume another analytical question; regenerated requests use the normal allowance. Saving while a response is running is rejected. Superseded clarification responses remain visible as collapsed audit history and are excluded from model context.

A one-character typo such as `g` keeps the same confirmation plan. `go ahead` after an unresolved revision re-presents the unchanged plan for a fresh confirmation; it never silently executes it. An unanswered genuine clarification remains open. A Run this plan button is bound to the current run and proposal.

Validation: full Python suite with PostgreSQL, targeted edit/approval regression tests, desktop and mobile Playwright checks, workspace browser suite, Ruff, and whitespace checks. Browser coverage includes cancel, failed-save draft retention, stable retries, and edited-copy navigation. These checks use deterministic local providers and synthetic data; they do not establish live-provider accuracy or production deployment.
