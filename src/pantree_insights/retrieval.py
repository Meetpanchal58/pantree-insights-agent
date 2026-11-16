"""Hybrid retrieval: find the tables, metrics, example queries and filter values a question needs.

Four small indexes, each searched two ways and fused with reciprocal-rank fusion (LlamaIndex):
  - BM25 keyword search: exact words such as "COD", "Diwali", "RTO"
  - dense embeddings (BAAI/bge-small-en-v1.5, run locally with fastembed): meaning, e.g.
    "money we keep after returns" -> realized_revenue

    tables    one document per allowed table (description + columns)
    metrics   one per metric in semantic/metrics.yaml
    examples  one per verified question -> SQL pair (the question is what is matched)
    values    one per distinct value of short text columns (e.g. sale_events.name = 'Diwali 2026'),
              so the SQL writer filters on values that really exist

Nothing here calls an LLM, and the indexes are rebuilt in a few seconds when the data or the
semantic files change.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import duckdb
from llama_index.core import Settings as LlamaSettings
from llama_index.core import VectorStoreIndex
from llama_index.core.llms import MockLLM
from llama_index.core.retrievers import QueryFusionRetriever
from llama_index.core.schema import NodeWithScore, TextNode
from llama_index.embeddings.fastembed import FastEmbedEmbedding
from llama_index.retrievers.bm25 import BM25Retriever

from .config import ROOT, get_settings
from .db import connect
from .semantic import (
    Example,
    Glossary,
    Metrics,
    Schema,
    load_examples,
    load_glossary,
    load_library,
    load_metrics,
    load_schema,
    tables_in,
)

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
MAX_DISTINCT_VALUES = 800                       # index text columns with at most this many values
SKIP_COLUMN = re.compile(r"(_id$|^id$|pincode|barcode|image_url|^utm_campaign$|^agent_name$|description|"
                         r"transcript|review_text|search_query)")
TOP_K = {"tables": 5, "metrics": 4, "examples": 4, "values": 8}
MAX_TABLES = 6          # tables passed to the agent (tuned on the regression suite only, evals/retrieval_eval.py)
TABLE_HITS = 3          # direct table-search hits added after tables implied by examples, metrics and values
LlamaSettings.llm = MockLLM()                   # retrieval never needs an LLM; avoid default OpenAI lookup


# ----------------------------------------------------------------------------- documents
def table_nodes(schema: Schema) -> list[TextNode]:
    nodes = []
    for name, t in schema.tables.items():
        columns = "; ".join(f"{c}: {d}" for c, d in t.columns.items())
        text = f"Table {name} ({t.group}). {t.grain}. {t.description} Columns: {columns}"
        nodes.append(TextNode(id_=f"table:{name}", text=text, metadata={"kind": "tables", "key": name},
                              excluded_embed_metadata_keys=["kind", "key"], excluded_llm_metadata_keys=["kind", "key"]))
    return nodes


def metric_nodes(metrics: Metrics) -> list[TextNode]:
    nodes = []
    for name, m in metrics.metrics.items():
        dims = ", ".join(metrics.sources[m.source].dimensions)
        text = f"Metric {name}: {m.label}. {m.description} Unit: {m.unit}. Breakdowns: {dims}. {m.notes or ''}"
        nodes.append(TextNode(id_=f"metric:{name}", text=text, metadata={"kind": "metrics", "key": name}))
    for name, c in metrics.custom_metrics.items():
        nodes.append(TextNode(id_=f"metric:{name}", text=f"Metric {name}: {c.label}. {c.description} Unit: {c.unit}.",
                              metadata={"kind": "metrics", "key": name}))
    return nodes


def example_nodes(examples: list[Example]) -> list[TextNode]:
    """One node per example; other phrasings of the question are part of the searchable text."""
    return [TextNode(id_=f"example:{e.id}",
                     text=" | ".join([e.question, *e.variants]) + (f" ({', '.join(e.tags)})" if e.tags else ""),
                     metadata={"kind": "examples", "key": e.id}) for e in examples]


def value_nodes(con: duckdb.DuckDBPyConnection, schema: Schema, glossary: Glossary | None = None) -> list[TextNode]:
    """One node per distinct value of short text columns; the same value in several columns is one node.
    Aliases from glossary.yaml are added to the text, so "cash on delivery" finds 'COD'."""
    aliases = (glossary or Glossary()).value_aliases
    where: dict[str, list[str]] = {}
    for table, info in schema.tables.items():
        for column, desc in info.columns.items():
            if not desc.startswith("text") or SKIP_COLUMN.search(column):
                continue
            n = con.execute(f'SELECT COUNT(DISTINCT "{column}") FROM {table}').fetchone()[0]
            if not 1 < n <= MAX_DISTINCT_VALUES:
                continue
            for (value,) in con.execute(f'SELECT DISTINCT "{column}" FROM {table} WHERE "{column}" IS NOT NULL'
                                        ).fetchall():
                where.setdefault(str(value), []).append(f"{table}.{column}")
    nodes = []
    for value, columns in where.items():
        readable = re.sub(r"[_-]+", " ", value)
        also = f" also called {', '.join(aliases[value])}" if value in aliases else ""
        key = hashlib.md5(value.encode()).hexdigest()[:12]
        nodes.append(TextNode(id_=f"value:{key}",
                              text=f"{readable}{also} ({', '.join(c.split('.')[1] for c in columns)})",
                              metadata={"kind": "values", "key": value, "columns": columns},
                              excluded_embed_metadata_keys=["kind", "key", "columns"],
                              excluded_llm_metadata_keys=["kind", "key", "columns"]))
    return nodes


# ----------------------------------------------------------------------------- the retriever
@dataclass
class Value:
    value: str
    columns: list[str]
    score: float


@dataclass
class Context:
    """What a question needs, ready to put into a prompt."""
    question: str
    tables: list[str]
    metrics: list[str]
    examples: list[Example]
    values: list[Value]
    joins: list[str] = field(default_factory=list)
    terms: dict[str, str] = field(default_factory=dict)

    def to_prompt(self, schema: Schema | None = None, metrics: Metrics | None = None) -> str:
        schema = schema or load_schema()
        metrics = metrics or load_metrics()
        parts = []
        if self.terms:
            terms = "\n".join(f"- {t}: {m}" for t, m in self.terms.items())
            parts.append("## Business terms in the question\n" + terms)
        parts.append("## Tables\n" + schema.describe(self.tables))
        if self.joins:
            parts.append("## How these tables join\n" + "\n".join(f"- {j}" for j in self.joins))
        if self.metrics:
            parts.append("## Metric definitions (use these exactly)\n" + metrics.describe(self.metrics))
        if self.values:
            parts.append("## Values that exist in the data (use these exact strings in filters)\n" + "\n".join(
                f"- '{v.value}' in {', '.join(v.columns)}" for v in self.values))
        if self.examples:
            parts.append("## Similar verified questions\n" + "\n\n".join(
                f"Q: {e.question}\nSQL:\n{e.sql.strip()}" + (f"\nNote: {e.notes}" if e.notes else "")
                for e in self.examples))
        return "\n\n".join(parts)


def embed_with_cache(nodes: list[TextNode], embed: FastEmbedEmbedding, cache_file: Path) -> None:
    """Attach embeddings to nodes, reusing vectors saved on disk (keyed by model + text)."""
    import numpy as np

    texts = [n.get_content(metadata_mode="embed") for n in nodes]
    keys = [hashlib.sha1(f"{EMBED_MODEL}\n{t}".encode()).hexdigest() for t in texts]
    cache: dict[str, list[float]] = {}
    if cache_file.exists():
        with np.load(cache_file) as saved:
            cache = dict(zip(saved["keys"].tolist(), saved["vectors"], strict=True))
    missing = [i for i, k in enumerate(keys) if k not in cache]
    if missing:
        for i, vector in zip(missing, embed.get_text_embedding_batch([texts[i] for i in missing]), strict=True):
            cache[keys[i]] = vector
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        live = {k: cache[k] for k in keys}                  # keep only what is still used
        np.savez(cache_file, keys=np.array(list(live)), vectors=np.array(list(live.values()), dtype=np.float32))
    for node, key in zip(nodes, keys, strict=True):
        node.embedding = [float(x) for x in cache[key]]


class Retriever:
    def __init__(self, con: duckdb.DuckDBPyConnection | None = None, schema: Schema | None = None,
                 metrics: Metrics | None = None, examples: list[Example] | None = None,
                 cache_dir: Path | None = None):
        self.schema = schema or load_schema()
        self.metrics = metrics or load_metrics()
        self.examples = {e.id: e for e in (examples if examples is not None
                                           else load_examples().examples + load_library())}
        self.glossary = load_glossary()
        con = con or connect()
        cache_dir = cache_dir or ROOT / ".cache"
        embed = FastEmbedEmbedding(model_name=EMBED_MODEL, cache_dir=str(cache_dir / "models"))
        groups = {"tables": table_nodes(self.schema), "metrics": metric_nodes(self.metrics),
                  "examples": example_nodes(list(self.examples.values())),
                  "values": value_nodes(con, self.schema, self.glossary)}
        embed_with_cache([n for nodes in groups.values() for n in nodes], embed, cache_dir / "embeddings.npz")
        self.sizes = {kind: len(nodes) for kind, nodes in groups.items()}
        self.values = {n.metadata["key"] for n in groups["values"]}     # every indexed value
        self.value_columns = {n.metadata["key"]: n.metadata["columns"] for n in groups["values"]}
        self._fusion = {}
        for kind, nodes in groups.items():
            k = TOP_K[kind] + 1                    # one spare, so a held-out example can be dropped
            vector = VectorStoreIndex(nodes, embed_model=embed, show_progress=False).as_retriever(similarity_top_k=k)
            keyword = BM25Retriever.from_defaults(nodes=nodes, similarity_top_k=min(k, len(nodes)))
            self._fusion[kind] = QueryFusionRetriever([vector, keyword], similarity_top_k=k, num_queries=1,
                                                      mode="reciprocal_rerank", use_async=False, llm=MockLLM())

    def _search(self, kind: str, question: str, exclude: set[str] = frozenset()) -> list[NodeWithScore]:
        hits = [h for h in self._fusion[kind].retrieve(question) if h.node.metadata["key"] not in exclude]
        return hits[:TOP_K[kind]]

    def retrieve(self, question: str, exclude_examples: set[str] = frozenset()) -> Context:
        tables = [h.node.metadata["key"] for h in self._search("tables", question)]
        metric_names = [h.node.metadata["key"] for h in self._search("metrics", question)]
        examples = [self.examples[h.node.metadata["key"]]
                    for h in self._search("examples", question, exclude_examples)]
        values = [Value(h.node.metadata["key"], h.node.metadata["columns"], round(h.score or 0.0, 4))
                  for h in self._search("values", question)]

        # Tables named by glossary phrases ("delivery region" -> customer_addresses) and those the examples,
        # metrics and values point to come first: they are the strongest signals.
        ordered: list[str] = self.glossary.tables_in(question)
        for e in examples[:2]:
            ordered += sorted(tables_in(e.sql) & set(self.schema.tables))
        for name in metric_names[:2]:
            ordered += self._metric_tables(name)
        for v in values[:3]:
            ordered += [c.split(".")[0] for c in v.columns[:2]]
        ordered += tables[:TABLE_HITS]
        selected = list(dict.fromkeys(t for t in ordered if t in self.schema.tables))[:MAX_TABLES]
        joins = [j for j in self.schema.joins
                 if all(side.strip().split(".")[0] in selected for side in j.split("="))]
        return Context(question, selected, metric_names, examples, values, joins, self.glossary.terms_in(question))

    def _metric_tables(self, name: str) -> list[str]:
        if name in self.metrics.metrics:
            sql = f"SELECT 1 FROM {self.metrics.sources[self.metrics.metrics[name].source].from_}"
        else:
            sql = self.metrics.custom_metrics[name].sql
        return sorted(tables_in(sql) & set(self.schema.tables))


@lru_cache
def get_retriever() -> Retriever:
    """One shared retriever per process (indexes are built once, in a few seconds)."""
    settings = get_settings()
    return Retriever(connect(settings.shop_dir))


def main() -> None:
    """pantree-context "question": show the context the SQL writer would receive."""
    import sys
    import time

    question = " ".join(sys.argv[1:]) or "How did Diwali 2026 do compared with 2025?"
    started = time.perf_counter()
    retriever = get_retriever()
    built = time.perf_counter()
    context = retriever.retrieve(question)
    print(f"# {question}\n# index built in {built - started:.1f}s, retrieved in "
          f"{(time.perf_counter() - built) * 1000:.0f} ms\n")
    print(context.to_prompt())


if __name__ == "__main__":
    main()
