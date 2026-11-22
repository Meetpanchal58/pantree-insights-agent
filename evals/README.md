> Evaluation now defaults to `--mode autonomous`: gold judges but never corrects the answer. Use
> `--mode assisted` for a separate stakeholder-guided run. Confirmation plans receive blind scripted “Go ahead” replies;
> unresolved clarification is incomplete. Mixed-mode
> reports are rejected. Numerical/lexical checks do not establish general narrative faithfulness.
> See [current validation](../docs/pr40-validation.md) for local evidence and unavailable live measurements.

# Evaluation

| File | What |
|---|---|
| `holdout.yaml` | unseen answer, clarify and decline questions with gold SQL |
| `behaviour.yaml` | valid questions that sound sensitive (must be answered) and paraphrased attacks (must be declined) |
| `coverage.yaml` | questions for tables and columns the other sets do not reach |
| `regression.yaml`, `questions.yaml` | regression set |
| `flow_regression.yaml` | follow-up questions with chat history |
| `gold_specs.yaml` | the intended request (spec or template) for every answer question; never shown to the model |

## Free checks

```bash
DATA_SIZE=full uv run python evals/run_eval.py --suite all --dry-run    # gold SQL runs, every answer has a gold spec
```

```bash
DATA_SIZE=full uv run python evals/spec_eval.py                         # every gold spec/template returns the gold
```

## Paid run

```bash
DATA_SIZE=full uv run python evals/run_eval.py --suite all --yes --max-cost-usd 4 --gate
```

Answer questions run with the **simulated stakeholder**: it answers clarifying questions as the asker would and
confirms a read-back only if it computes the intended spec, otherwise it states what it wants. Behaviour sets run
as before (vague questions must be clarified, unsafe requests declined, clear questions not over-asked).

Report measures: correct answers; **verified answers equal to the gold (gate 100%)**; **silent wrong answers (gate
0)**; first reading correct; clarifying questions per answer; behaviour sets; latency and cost. Rows are saved as
they finish (`*.partial.jsonl`); `--rescore` re-scores a saved run for free.
