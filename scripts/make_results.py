"""Print auditable Markdown metrics from one fresh evaluation report; makes no model calls."""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'evals'))
from provenance import latency  # noqa: E402
from stats import summarize  # noqa: E402


def percent(value):
    return f'{math.floor(value*1000)/10:.1f}%'


def upper(value, places):
    return f"{math.ceil(value * 10**places) / 10**places:.{places}f}"


def rate(rows, key):
    if not rows:
        return '— (n=0)'
    result = summarize(rows, key)
    if result['mean'] is None:
        return 'unavailable'
    lo, hi = result['ci']
    return (f"{sum(r.get(key) is True for r in rows)}/{len(rows)} · {percent(result['mean'])} "
            f"[{percent(lo)}–{math.ceil(hi*1000)/10:.1f}%]")


def clarification_count(row):
    # The legacy questions field counts answered clarifications, not unanswered autonomous prompts.
    return sum(max(1, len(e.get('questions') or [])) for e in row.get('trace', [])
               if e.get('type') == 'clarify' and e.get('kind') != 'confirm')


def report(rows, compiler=None, budget=None):
    if len({r.get('evaluation_mode') for r in rows}) > 1:
        raise ValueError('Do not combine autonomous and assisted evaluation rows')
    out = ['# Fresh evaluation results', '',
           '| Suite | N (all) | Correct answer tables (95% CI) | Verified = gold (95% CI) | Silent wrong | '
            'Correctly clarified (95% CI) | Correctly declined (95% CI) | Avg clarifying Qs | '
            'p50 / p95 seconds | Cost/question |',
           '| --- | ---: | --- | --- | ---: | --- | --- | ---: | --- | --- |']
    for suite in ['all', *sorted({r['suite'] for r in rows})]:
        part = rows if suite == 'all' else [r for r in rows if r['suite'] == suite]
        if not part:
            continue
        answers = [r for r in part if r['set'] == 'answer']
        verified = [r for r in answers if r.get('status') == 'done' and r.get('trust') == 'verified']
        clarify = [r for r in part if r['set'] == 'clarify']
        declines = [r for r in part if r['set'] in ('decline','adversarial')]
        timing = latency([r['seconds'] for r in part])
        known = [r['cost'] for r in part if r.get('cost') is not None and
                 all(c.get('cost_known', True) for c in r.get('calls', []))]
        cost = f'${upper(statistics.mean(known),5)} ({len(known)}/{len(part)} known)' if known else 'unavailable'
        out.append(f"| {suite} | {len(part)} | {rate(answers,'result_correct')} | {rate(verified,'result_correct')} | "
                   f"{sum(r.get('failure_type') == 'silent_wrong' for r in part)} | {rate(clarify,'passed')} | "
                   f"{rate(declines,'passed')} | {upper(statistics.mean(clarification_count(r) for r in part),2)} | "
                   f"{upper(timing['p50'],3)} / {upper(timing['p95'],3)} | {cost} |")
    out += ['', 'N includes every attempted case. Correctness uses answer cases only; '
            'routing categories have their own denominators.',
            'Clarification counts include unanswered questions and exclude confirmation prompts. '
            'Costs with missing usage are not treated as zero.',
            'Point percentages are rounded down; interval bounds are rounded outward. '
            'Intervals bootstrap questions; all-pass/all-fail boundaries use Wilson intervals.',
            'Cells with fewer than 30 cases have small N. One repeat does not measure run-to-run stability.',
            'Latency excludes analyst preparation, browser/network time, and human confirmation time. '
            'Calls share provider and SQL caches.',
            'These are simulated-data results. Narrative faithfulness and causal correctness are not measured.']
    if budget:
        out += ['', f"Planned: {budget['planned']}; recorded: {len(rows)}. Budget stopped: {budget['budget_stopped']}.",
                f"Known cost: ${budget['known_usd']:.6f}; held for uncertain calls: ${budget['held_usd']:.6f}; "
                f"ceiling: ${budget['ceiling_usd']:.2f}.",
                'The ledger includes billed or uncertain calls even when an answer did not finish. '
                'Its total takes precedence over row-level cost averages.']
    if compiler:
        entries = compiler['rows']
        timing = compiler['query_latency']
        out += ['', '## Free compiler baseline', '',
                f"{sum(r['ok'] for r in entries)}/{len(entries)} gold-spec results match gold SQL "
                f"across {compiler['cases']} cases × {compiler['repeats']} passes. Model calls: 0.",
                f"DuckDB p50 / p95: {upper(timing['p50'],4)} / {upper(timing['p95'],4)} seconds. "
                f"Snapshot unchanged: {compiler['snapshot_verified']}.",
                'This checks compilation from intended specifications, not natural-language understanding.']
    out += ['', '## Failure analysis', '']
    failures = [r for r in rows if r.get('failure_type')]
    if not failures:
        out.append('No observed failure types in this report; this is not evidence that failures are impossible.')
    for kind, count in Counter(r['failure_type'] for r in failures).most_common(3):
        example = next(r for r in failures if r['failure_type'] == kind)
        question = example['question'].replace('\n',' ')
        reason = example.get('reason','').replace('\n',' ')
        if kind == 'unresolved' and example.get('evaluation_mode') == 'autonomous':
            reason = 'Requested clarification; autonomous mode supplies no business-choice answers.'
        out.append(f"- **{kind} ({count})**: `{example['suite']}/{example['id']}` — {question} Reason: {reason}")
    return '\n'.join(out)+'\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--compiler', type=Path)
    parser.add_argument('--budget', type=Path)
    args = parser.parse_args()
    def load(p):
        return json.loads(p.read_text()) if p else None
    print(report(load(args.report), load(args.compiler), load(args.budget)), end='')


if __name__ == '__main__':
    main()
