"""Offline metric-context ablation. Gold specs are evaluation inputs, never production prompt examples.

This measures required-metric recall and context size, not interpretation accuracy. A lexical retriever
that misses a requested metric cannot safely replace the complete catalog without a fallback.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter

from run_eval import load_suite
from spec_eval import load_gold_specs

from pantree_insights.fastpath import METRICS
from pantree_insights.interpret import catalog_text
from pantree_insights.semantic import load_glossary
from pantree_insights.spec import default_catalog


def tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower().replace('_', ' '))


def main():
    cat, gold = default_catalog(), load_gold_specs()
    documents = {name: tokens(' '.join([name, m.label, m.description,
                  *[alias for alias, target in METRICS.items() if target == name]]))
                 for name, m in cat.metrics.metrics.items()}
    df = Counter(t for doc in documents.values() for t in set(doc))
    average = sum(map(len, documents.values())) / len(documents)
    rows = []
    for item in load_suite('all'):
        spec = gold.get(f"{item['suite']}/{item['id']}", {})
        required = {m['metric'] for m in spec.get('measures', []) if m.get('metric')}
        if not required:
            continue
        query = set(tokens(item['question']))
        def score(doc, mode, query=query):
            counts = Counter(doc)
            if mode == 'keyword':
                return len(query & set(doc))
            return sum(math.log(1 + (len(documents) - df[t] + 0.5) / (df[t] + 0.5)) *
                       counts[t] * 2.2 / (counts[t] + 1.2 * (0.25 + 0.75 * len(doc) / average))
                       for t in query if counts[t])
        for mode in ('keyword', 'bm25'):
            for k in (5, 10):
                ranked = sorted(documents, key=lambda n: (-score(documents[n], mode), n))[:k]
                rows.append({'id': f"{item['suite']}/{item['id']}", 'mode': mode, 'k': k,
                             'complete': required <= set(ranked), 'missing': sorted(required-set(ranked))})
    summary = []
    for mode in ('keyword', 'bm25'):
        for k in (5, 10):
            part = [r for r in rows if r['mode'] == mode and r['k'] == k]
            summary.append({'mode': mode, 'metrics_supplied': k, 'complete_cases': sum(r['complete'] for r in part),
                            'cases': len(part), 'misses': [r for r in part if not r['complete']]})
    print(json.dumps({'kind': 'offline_metric_recall_only', 'metrics': len(documents),
                      'catalog_characters': len(catalog_text(cat, load_glossary().terms)),
                      'full_catalog_complete_cases': len(rows)//4, 'summary': summary,
                      'vectors': 'not measured: no embedding credentials or validated accuracy baseline',
                      'decision': 'retain full context on the model path; reviewed grammar uses no catalog prompt'},
                     indent=2))


if __name__ == '__main__':
    main()
