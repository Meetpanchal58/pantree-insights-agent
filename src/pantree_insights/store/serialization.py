"""Store: serialization."""
from __future__ import annotations

import json
from typing import Any


def to_json(value: Any) -> Any:
    """Make DB rows JSON-friendly (datetimes as ISO strings)."""
    return json.loads(json.dumps(value, default=lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v)))
