# Pantree Insights Agent

A verified AI data analyst for Pantree, a simulated Indian online fashion shop (catalogue, customers, visits,
orders, returns, marketing, stock, support). Ask a business question in plain English; get a verified number,
the SQL behind it, a chart and an explanation.

### [▶ Live demo](https://pantree-insights-um7bylhdra-uc.a.run.app/)

## How it answers

1. **Understands** the question against the shop's catalog and business definitions, and turns it into a
   structured request: metric, splits, filters, period.
2. **Asks** when something is genuinely open: a phrase with two meanings, or two readings that give different
   numbers.
3. **Confirms**: shows exactly what it will compute (definitions, filters, period); nothing runs until you agree.
4. **Computes** with SQL built by code from the governed definitions (or a reviewed analysis template). No model
   writes SQL.
5. **Explains** the result; every number in the text is filled in from the result by code.

Every answer is labelled **Verified**. A question it cannot answer that way gets "I can't answer this as a verified
number yet", never a guess. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Repository

```
src/pantree_insights/   graph.py (flow) · interpret.py · spec.py (compiler) · templates.py · agents.py
                        catalog.py · semantic.py · joins.py · profile.py · guardrails.py · grounding.py · db.py
                        web.py · auth.py · store.py · memory.py · pulse.py · charts.py · demos.py · static/
semantic/               schema, metrics, concepts, glossary (incl. two-meaning phrases), generated catalog
prompts/agents.yaml     interpreter, contrast, analyst
evals/                  questions, gold SQL, gold specs, runner with the simulated stakeholder, free compiler check
tests/                  1,400+ tests: compiler vs gold and vs an independent implementation, every flow route,
                        guardrails, knowledge-base lint, web app
```

## Quickstart

Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run pantree-data download --size full      # ~470 MB; --size small (~170 MB) for a quick start
```

```bash
uv run pantree-ask "How did Diwali 2026 do compared with Diwali 2025?"   # needs OPENAI_API_KEY in .env
```

```bash
uv run pantree-web                            # the app at http://127.0.0.1:8000
```

```bash
uv run pantree-catalog                        # regenerate semantic/catalog.yaml after a schema change
```

Settings come from environment variables or `.env` (see `.env.example`).

## Checks

```bash
uv run pytest -q
```

```bash
DATA_SIZE=full uv run python evals/spec_eval.py           # every gold question compiles to the gold answer (free)
```

```bash
DATA_SIZE=full uv run python evals/run_eval.py --suite all --yes --max-cost-usd 4 --gate   # paid, see evals/README.md
```

## Deploy

Google Cloud Run, Neon Postgres, Google sign-in and email codes, data from Cloud Storage:
[docs/deploy.md](docs/deploy.md).

## Limitations

- Simulated data: numbers describe the simulator, not a real shop.
- Questions outside the request format and the analysis templates are declined until a template is added.
- The confirmation step relies on the user reading the read-back; it states exact definitions to make that easy.

## License

MIT
