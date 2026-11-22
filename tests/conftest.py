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


@pytest.fixture
def template_params():
    dated = {"start": "2025-01-01", "end": "2025-03-01"}
    return {"cohort_retention": {**dated, "as_of": "2025-06-01", "days": 30},
            "session_funnel": {**dated, "steps": ["pdp_view", "add_to_cart", "purchase"]},
            "customer_segments": dated}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    """Classify transitive data fixtures and skip unavailable snapshots before setup."""
    missing = not (SMALL / "manifest.json").is_file()
    unavailable = pytest.mark.skip(reason="small shop not downloaded (pantree-data download --size small)")
    for item in items:
        if "shop_dir" in item.fixturenames and not item.get_closest_marker("data"):
            item.add_marker(pytest.mark.data)
        if missing and item.get_closest_marker("data"):
            item.add_marker(unavailable)
