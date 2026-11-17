"""Collect externally authored questions without pretending model-written questions are external.

Usage:
    python evals/collect_external.py --author alice --question "Why did revenue fall last month?"

This appends only the question/author/intake metadata. Gold SQL and annotations must
be reviewed separately before the item is eligible for scoring.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
PATH = ROOT / "external_intake.yaml"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--author", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--decision", default="")
    args = parser.parse_args()

    data = yaml.safe_load(PATH.read_text()) if PATH.exists() else {"questions": []}
    data.setdefault("questions", []).append({
        "author": args.author.strip(),
        "question": args.question.strip(),
        "decision_informed": args.decision.strip(),
        "submitted_at": datetime.now(UTC).isoformat(),
        "reviewed": False,
    })
    PATH.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    print(f"Added external question #{len(data['questions'])}; review it before moving it to holdout_external.yaml.")


if __name__ == "__main__":
    main()
