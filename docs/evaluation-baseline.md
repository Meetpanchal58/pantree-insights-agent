# Baseline before hybrid retrieval and generated SQL

This is the first phase of the retrieval/SQL plan. It establishes reference calculations, stricter scoring
and reproducible timing evidence. It does not enable vector retrieval or a free-form SQL writer.

## Calculation corrections

- A filter on a named metric applies to every aggregate in its definition, including both sides of a ratio.
  Other measures in the same query retain their own populations. The read-back includes these filters.
- A filter selecting sale-event windows uses membership checks. Overlapping events cannot duplicate a sale
  in the filtered total. Event breakdowns still attribute a sale to each matching event, so those groups are
  not additive across overlapping events.
- Event lift divides by calendar days, including days with no sales. Windows must fit within the recorded
  Sale-line date range. This is a conservative coverage proxy, not proof of source completeness; production
  coverage metadata belongs in the next knowledge-base phase. An empty baseline yields a null lift.

## Free baseline

From a checkout of this PR, with the small dataset already downloaded:

```bash
uv sync --locked
DATA_SIZE=small uv run python evals/run_eval.py --suite all --dry-run
DATA_SIZE=small uv run python evals/baseline.py --repeats 3
```

The baseline runs every gold spec/template against independently written gold SQL. It saves timestamped
JSON and Markdown under `evals/reports/`; CI uploads them as `compiler-baseline-<commit>` artifacts.
It records setup time, compilation time and DuckDB query p50/p95/max with sample counts. Passes share a
connection and OS caches. This is a local compiler/SQL benchmark, not an application cold-start or load test.
It cannot establish model understanding, retrieval quality, insight accuracy or Cloud Run latency.

Inputs include the commit, dirty-tree flag, source digest, case/gold digest, complete dataset content digest,
manifest, Python/package versions and relevant model/query settings. Dataset contents are checked again at
completion; a changed dataset invalidates the run. Use immutable versioned datasets: hashing before and after
does not provide an atomic snapshot of files being overwritten during a run. Data hashing is outside timings.

## Paid model smoke test

Configure the provider key privately in the shell environment. Preview the historical cost estimate first:

```bash
DATA_SIZE=small uv run python evals/run_eval.py --suite flow --repeats 3
```

Then explicitly start the model calls, with a USD 1 run cap and the configured daily cap still applying:

```bash
DATA_SIZE=small uv run python evals/run_eval.py --suite flow --repeats 3 --max-cost-usd 1 --gate --yes
```

One in-flight provider call may exceed the spend cap. Budget exhaustion leaves an incomplete report and fails
the gate. Cases use isolated memory; the agent's SQL cache is shared and provider prompt caching is uncontrolled.
Reports include per-stage timings and model call usage, with preparation separate from request latency.
Failed requests remain in the accuracy denominator. Unknown cost is reported as unknown, not zero.

The stakeholder simulation can correct an interpretation using the intended spec. Therefore result-table
accuracy after those corrections is not first-turn accuracy. Reports separately show first readings, questions
and corrections. Behaviour-only passes test routing, not numerical correctness. Narrative claims need a separate
independent evaluation before claiming end-to-end answer accuracy.

## Scoring and release gate

- Every inferred numeric reference column is checked, not only the chart's preferred metric.
- Integer counts match exactly; floats use 1e-9 relative / 1e-6 absolute tolerance by default.
- Different justified rounding tolerances must be explicit in a case's `metric_tolerances`, e.g.
  `metric_tolerances: {revenue: {rtol: 0, atol: 0.01}}`.
- Multiplying a ratio by 100 is accepted only for explicitly annotated `percent_metrics`.
- Truncated gold or generated results cannot pass. The event-lift reference returns all requested events.
- Errors, unresolved runs and wrong answers fail. For over-asking cases, a valid confirmation screen is a routing
  success; it is not counted as a completed numerical answer. All requested cases must pass `--gate`.
- Every clarification/confirmation interaction counts toward the bounded stakeholder turn limit.
- Confidence intervals bootstrap questions, with Wilson intervals at all-pass/all-fail boundaries. Unequal repeat
  counts do not produce a pass-k claim. These intervals describe the benchmark sample, not universal reliability.

Keep the original report when rescoring:

```bash
DATA_SIZE=small uv run python evals/run_eval.py --rescore evals/reports/<report>.json
```

Rescoring requires matching dataset and case fingerprints, writes a separate `.rescored.json`/`.md` pair,
and refuses legacy unpinned reports. Do not compare historical 1%-tolerance scores directly with this baseline.

## Next phases

Version the business knowledge records, then benchmark keyword, vector and hybrid retrieval against the same
held-out questions and context budget. Follow with validated generated SQL, structured multi-turn state and
concurrent live tests. Retrieval metrics remain unavailable in this baseline; they are not reported as zero or
as passing. The four offline demos remain model-free.
