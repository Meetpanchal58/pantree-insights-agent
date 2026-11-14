import os
from pathlib import Path

import pytest

from pantree_insights.config import ROOT

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
