import os
from pathlib import Path

import pytest

# Tests run on the small shop (the one CI downloads) unless DATA_SIZE is set explicitly, e.g. DATA_SIZE=full.
os.environ.setdefault("DATA_SIZE", "small")

from pantree_insights.config import ROOT  # noqa: E402

SMALL = Path(os.environ.get("DATA_DIR", ROOT / "data")) / "small"


@pytest.fixture(scope="session")
def shop_dir() -> Path:
    if not (SMALL / "manifest.json").exists():
        pytest.skip("small shop not downloaded (pantree-data download --size small)")
    return SMALL


@pytest.fixture(scope="session")
def con(shop_dir):
    from pantree_insights.db import connect

    return connect(shop_dir)
