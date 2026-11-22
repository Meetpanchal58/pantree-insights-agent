"""Run one reproducible sample with a cumulative, pre-call spending reservation."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import threading
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'evals')]

from make_results import report  # noqa: E402
from run_eval import load_suite, run  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from pantree_insights.config import get_settings  # noqa: E402
from pantree_insights.llm import BudgetExceeded, ChatLLM  # noqa: E402
from pantree_insights.store import Store, api_calls  # noqa: E402


def sample(items: list[dict], size: int, seed: int) -> list[dict]:
    """Proportional sampling by suite/set, at least one case per stratum; no outcome-based selection."""
    groups = defaultdict(list)
    for item in items:
        groups[item['suite'], item['set']].append(item)
    if not len(groups) <= size <= len(items):
        raise ValueError(f'questions must be between {len(groups)} and {len(items)}')
    remaining = size - len(groups)
    weights = {key: len(values)-1 for key, values in groups.items()}
    total = sum(weights.values())
    exact = {key: remaining * weight / total if total else 0 for key, weight in weights.items()}
    counts = {key: 1 + math.floor(value) for key, value in exact.items()}
    for key in sorted(groups, key=lambda k: (-(exact[k]-math.floor(exact[k])), k))[:size-sum(counts.values())]:
        counts[key] += 1
    def order(item):
        return hashlib.sha256(f"{seed}:{item['suite']}:{item['set']}:{item['id']}".encode()).hexdigest()
    selected = [item for key, values in groups.items() for item in sorted(values, key=order)[:counts[key]]]
    return sorted(selected, key=order)


class EvaluationStore(Store):
    """Isolated ledger; uncertain calls remain charged, including across a UTC day change."""
    def __init__(self, url: str, ceiling: float):
        super().__init__(url)
        self.ceiling = ceiling
        self.stopped = False
        self.admission = threading.Lock()

    def totals(self):
        with self.engine.connect() as con:
            rows = con.execute(select(api_calls)).mappings().all()
        return {'known_usd': sum(r['usd'] or 0 for r in rows),
                'held_usd': sum(r['reserved_usd'] or 0 for r in rows if r['usd'] is None),
                'calls': len(rows), 'uncertain_calls': sum(r['usd'] is None for r in rows)}

    def reserve_api_call(self, pin, run_id, agent, amount):
        with self.admission:
            with self.engine.connect() as con:
                spent = con.execute(select(func.coalesce(func.sum(
                    func.coalesce(api_calls.c.usd, api_calls.c.reserved_usd)), 0))).scalar_one()
            if spent + amount > self.ceiling:
                self.stopped = True
                raise BudgetExceeded('Evaluation spending ceiling cannot cover the next call.')
            try:
                return super().reserve_api_call(pin, run_id, agent, amount)
            except BudgetExceeded:
                self.stopped = True
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--questions', type=int, default=100)
    parser.add_argument('--seed', type=int, default=20261122)
    parser.add_argument('--max-cost-usd', type=float, default=4)
    parser.add_argument('--mode', choices=['autonomous'], default='autonomous')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--output-dir', type=Path, default=ROOT/'evals/reports/current')
    parser.add_argument('--ledger', type=Path, default=ROOT/'.cache/fresh-evaluation.db')
    args = parser.parse_args()
    if not math.isfinite(args.max_cost_usd) or not 0 < args.max_cost_usd <= 4:
        parser.error('max-cost-usd must be positive and at most 4')
    items = sample(load_suite('all'), args.questions, args.seed)
    if (args.output_dir/'paid.json').exists() or (args.output_dir/'paid.partial.jsonl').exists():
        parser.error('A paid run already exists here; do not overwrite or silently repeat paid work')
    if args.yes and args.ledger.exists():
        parser.error('Ledger already exists; inspect it before starting another paid run')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    selection = {'seed': args.seed, 'planned': len(items), 'sampling': 'proportional suite/set strata, minimum one',
                 'cases': [{k: i[k] for k in ('suite','set','id','question')} for i in items]}
    (args.output_dir/'selection.json').write_text(json.dumps(selection, indent=2)+'\n')
    if not args.yes:
        print(f'{len(items)} selected questions; ceiling ${args.max_cost_usd:.2f}. Use --yes for paid calls.')
        return
    if not os.environ.get('OPENAI_API_KEY'):
        parser.error('OPENAI_API_KEY must be supplied securely in the environment')
    settings = get_settings()
    if not settings.model_main.startswith('openai:'):
        parser.error('This fresh-run command currently supports the configured OpenAI model only')
    settings.daily_budget_usd = args.max_cost_usd
    settings.daily_user_budget_usd = args.max_cost_usd
    settings.llm_max_retries = 0
    settings.max_parallel_llm_calls = 1
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    store = EvaluationStore(f'sqlite:///{args.ledger.resolve()}', args.max_cost_usd)
    user = store.upsert_user('evaluation@example.test')
    pin = store.pin_ai(user)
    def factory():
        return ChatLLM(selection=pin, store=store, run_id=uuid.uuid4().hex)
    live = args.output_dir/'paid.partial.jsonl'
    started = datetime.now(UTC).isoformat()
    try:
        rows = run(items, live, mode=args.mode, llm_factory=factory, should_stop=lambda: store.stopped)
        (args.output_dir/'paid.json').write_text(json.dumps(rows, indent=1, default=str)+'\n')
        live.unlink(missing_ok=True)
    finally:
        metadata = {**selection, 'started_at': started, 'finished_at': datetime.now(UTC).isoformat(),
                    'ceiling_usd': args.max_cost_usd, 'budget_stopped': store.stopped,
                    'adapter': 'production managed structured-output adapter; retries disabled',
                    'model': settings.model_main, 'max_output_tokens': settings.max_output_tokens,
                    'reasoning_effort': settings.reasoning_effort, 'prices': settings.prices,
                    **store.totals()}
        (args.output_dir/'paid-budget.json').write_text(json.dumps(metadata, indent=2)+'\n')
        store.engine.dispose()
    compiler_path = args.output_dir/'compiler.json'
    compiler = json.loads(compiler_path.read_text()) if compiler_path.exists() else None
    (args.output_dir/'results.md').write_text(report(rows, compiler, metadata))
    print(json.dumps({k: metadata[k] for k in ('known_usd','held_usd','uncertain_calls','budget_stopped')}))


if __name__ == '__main__':
    main()
