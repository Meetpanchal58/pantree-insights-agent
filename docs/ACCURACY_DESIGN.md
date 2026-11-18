# Accuracy design: no wrong number reaches the user

Status: proposed (Nov 18, 2026). Builds on docs/ARCHITECTURE_V2.md (phases 1-2 built and measured).
The client is the real user. The simulated stakeholder in section 5 exists only to test the system.

## 1. The guarantee, stated precisely

> Every number the user is shown is correct for the confirmed question, the governed definitions and the data
> snapshot. When the system cannot reach that bar, it asks or says it cannot answer. It never guesses.

"100%" is achievable as this guarantee, not as "every question gets an answer". A question can end in three ways
only: a **verified answer**, a **question back to the user**, or **"I can't answer this as a verified number yet"**
(with what it can answer instead). There is no fourth way, so there is no path for a plausible wrong number.

What the guarantee depends on, and how each is protected:

| Depends on | Protection |
|---|---|
| The user confirms the right reading | the read-back states exact definitions, filters and period; known two-meaning phrases are always asked |
| The definitions are right | governed semantic layer: reviewed changes, tests, a lint for one-sided or conflicting entries |
| The SQL matches the definitions | deterministic compiler + templates; no model-written SQL in production answers; every construct tested |
| The data is right | read-only snapshot with an id; known data issues listed and shown as caveats |

## 2. Layer by layer: what can go wrong, and the control

| # | Layer | What could go wrong | Control (impossible or caught) |
|---|---|---|---|
| 0 | Input guard | unsafe request, injection | code rules + interpreter decline categories; tested by 19 decline/adversarial items |
| 1 | Interpretation | a two-meaning phrase read one way | **ambiguity register**: phrases with several meanings in this shop ("wrong size", "worst", "best", "active customer", "sales") always produce a question, whatever the model thinks |
| 1 | | a misreading the model is sure of | two independent readings must agree; the **read-back** shows the exact definition used ("share of delivered lines returned with reason Size Issue") and must be confirmed |
| 1 | | a term the catalog lacks | validation fails -> the user is asked; the term is logged for the knowledge base |
| 1 | | a question the spec cannot express | goes to a verified template, or ends as "can't answer verified yet" (never free SQL in stakeholder mode) |
| 2 | Validation | invented column, value, operator | every name and value checked against the catalog; values normalised to their stored spelling |
| 2 | | a period outside the data (Dec 2024) | **coverage check**: "the data starts on 2025-01-01", not a silent zero |
| 2 | | a join that repeats rows | refused by the join graph (measured cardinality) |
| 3 | Compilation | SQL that differs from the definition | deterministic compiler from metrics.yaml; tested on every gold answer and on every metric x dimension against an independent recompute |
| 3 | | names several IDs share (Payday Sale x12) | grouped with their ID automatically |
| 4 | Templates | multi-step logic wrong | each template is reviewed SQL with its own gold tests; parameters validated like specs |
| 5 | Execution | truncated or empty result misread | row caps flagged; empty results explained (no rows vs no data in range) |
| 6 | Narration | a number typed by the model | numbers exist only as claims rendered by code from the result; a sentence with an unverifiable number is removed |
| 6 | | a cause asserted the data does not show | analyst rules + caveats from metric notes added by code |
| 7 | Knowledge base | a wrong or one-sided entry ("wrong size" -> Size Issue) | **knowledge-base lint**: aliases map to one value only; terms with several meanings must be in the ambiguity register; every change runs the full free test suite |
| 8 | Modes | an unverified answer presented as fact | **stakeholder mode = verified only** (exploratory switched off); analyst mode shows exploratory answers with a red label and the SQL |
| 9 | Monitoring | a wrong answer slips through anyway | "this looks wrong" button -> incident with the full trace -> root cause -> a test that fails before the fix |

## 3. The user loop, end to end

```
question
 -> interpreter: spec, or questions, or "needs a template", or decline
 -> ambiguity register + validation + agreement     (any doubt -> question to the user)
 -> read-back with exact definitions                (the user confirms or corrects; corrections loop back)
 -> compile / template -> run -> verified table
 -> narration with code-rendered numbers
```

Questions to the user are short, concrete and few: 2-4 options, a recommended default, one line on why it changes the
numbers. Nothing the knowledge base settles is asked (it is answered from the definition and cited).

## 4. Why the model does not write SQL for answers

Measured on this project: with the requirement already resolved, the model's SQL was still wrong on some questions
(a missing filter, a missing share), and retries could turn a correct query into a wrong one. The compiler turns the
confirmed spec into the same SQL every time, is tested exhaustively and costs nothing. The model's strength,
language, is used where it matters: understanding the question and writing the explanation.

## 5. Testing the user loop (test only): the simulated stakeholder

Unattended evaluations used to auto-confirm every read-back, which measured the system with the loop switched off.
The simulated stakeholder knows each test question's intent (its gold spec) and plays the user:

- answers clarifying questions with the option that matches the intent;
- confirms a read-back only if the spec equals the intended spec; otherwise rejects it with the correction a user
  would give (e.g. "I mean items that did not fit, not the return reason");
- never sees or helps the interpreter beyond what a real user would say.

It is never deployed. In production the user plays this role.

## 6. How accuracy is reported (release gate)

| Measure | Gate |
|---|---|
| Verified answers equal to the gold answer | **100%** (any miss blocks release and gets a root cause) |
| Silent wrong answers (a wrong number not labelled or asked) | **0** |
| Questions the loop resolved correctly (with the simulated stakeholder) | reported; target 95%+ |
| Coverage: answered as verified without help | reported; grows with templates |
| Questions asked per answer | reported; target under 1 on clear questions |

## 7. Build order

1. Ambiguity register, knowledge-base lint, coverage check, routing rules, agreement that tolerates supporting
   columns, shorter narration, verified-only mode; the two test fixes and the scorer label fix.
2. Simulated stakeholder in the evaluation runner.
3. Verified templates: event lift vs baseline, repeat-purchase windows, measures from two tables.
4. Serving: worker pool, queue position, result cache keyed by spec and snapshot, streaming.
5. Free checks green -> paid run on the failed and remaining questions -> full release run.
