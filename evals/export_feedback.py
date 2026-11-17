"""Export negative production feedback as a review queue, never as automatic training data."""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from pantree_insights.config import ROOT
from pantree_insights.store import get_store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".cache" / "feedback_review.yaml")
    args = parser.parse_args()

    rows = []
    for item in get_store().feedback_review():
        final = next((e for e in reversed(item.get("events") or []) if e.get("type") == "final"), {})
        rows.append({
            "feedback_id": item["id"],
            "run_id": item["run_id"],
            "question": item["question"],
            "reason": item.get("reason") or "",
            "note": item.get("note") or "",
            "status": final.get("status"),
            "sql": final.get("sql"),
            "headline": (final.get("analysis") or {}).get("headline"),
            "reviewed": False,
        })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump({"feedback": rows}, sort_keys=False, allow_unicode=True))
    print(f"Exported {len(rows)} negative-feedback runs to {args.output}")


if __name__ == "__main__":
    main()
