# Audit of verified-labelled reference mismatches

The automatic score reports seven silent-wrong cases: a completed answer marked verified whose table does not match reference SQL. The score remains **7**, and the all-pass gate remains failed. This audit does not replace the preselected rubric or produce a post-hoc adjusted accuracy.

Both saved SQL and reference SQL were rerun locally against the unchanged snapshot, without model calls.

| Case | Observed mismatch | Interpretation and next step |
| --- | --- | --- |
| regression/h05 | The plan computes realized sales revenue, while the question asks for last-click credited campaign revenue. The top campaign population differs. | Clear metric/source interpretation failure. Strengthen explicit attribution-term checks before confirmation. |
| holdout/x11 | The query filters to COD, returning one row. The question compares COD with prepaid; the reference returns both. | Clear comparison-scope failure. Validate requested comparison members before execution. |
| regression/m06 | Both RTO rates match exactly. The response includes order counts; the strict reference requires shipped-line counts instead. | Extra-column contract mismatch. Do not claim the displayed rate is incorrect; review which columns are required by the question. |
| regression/m08 | Both queries return the same six out-of-stock active SKUs. The response omits inbound quantity, which is a numeric reference column. | Extra-column contract mismatch. Separate required stock membership from optional explanatory inventory fields in a future rubric. |
| regression/n10 | The response returns the single best surface, home_recs, with the matching CTR. The reference includes four ranked surfaces. | Ranking-extent mismatch: “best” can reasonably mean one winner. Review expected scope before a future run. |
| regression/e05 | Prime share matches exactly. The response omits the customer-count column required by the strict reference. | Extra-column contract mismatch. Define whether a denominator must be displayed as well as used. |
| regression/m11 | The response counts contact rows and shows ten categories; the reference counts distinct tickets and shows sixteen, with an extra response-time metric. CSAT values for matching categories agree. | Grain and scope ambiguity plus extra reference requirements. Clarify whether the unit is contacts or tickets and define the requested ranking extent. |

No failed case was retried, replaced, or reclassified in the published score. A future evaluation should distinguish required answer fields from optional reference columns before running models again.

The largest overall failure type is unresolved clarification. Autonomous mode supplies blind plan confirmations but no business-choice answers, so these cases count as incomplete even when asking a question is a reasonable product action. Over-asking and incorrect refusals are reported separately in [results.md](results.md).
