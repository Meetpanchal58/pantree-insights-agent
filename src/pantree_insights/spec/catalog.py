"""Spec: catalog."""
from __future__ import annotations

from functools import lru_cache

from ..joins import JoinGraph
from .models import Catalog


@lru_cache
def default_catalog(con=None) -> Catalog:
    from ..catalog import label_columns, load_catalog
    from ..db import connect
    from ..profile import build_profile
    from ..semantic import load_concepts, load_metrics, load_schema

    con = con or connect()
    schema = load_schema()
    labels = {k: v for k, v in label_columns().items()}
    return Catalog(schema, load_metrics(), load_concepts(), JoinGraph(schema, build_profile(con, schema)),
                   load_catalog(), labels, con)
