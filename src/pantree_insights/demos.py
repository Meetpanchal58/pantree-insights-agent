"""Demo runs: real agent runs recorded once, replayed to visitors with their original timing (no API cost).

    pantree-demo record "How did Diwali 2026 do compared with Diwali 2025?"
    pantree-demo list

Recordings live in demo_runs/ (committed to git) and keep their data date, so their numbers never look
wrong when the data moves on.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from .config import get_settings


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def demos(folder: Path | None = None) -> list[dict]:
    folder = folder or get_settings().demo_dir
    out = []
    for path in sorted(folder.glob("*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"slug": path.stem, "question": d["question"], "recorded_at": d.get("recorded_at"),
                    "topic": d.get("topic"), "offline": d.get("offline", False),
                    "data_size": d.get("data_size"), "data_as_of": d.get("data_as_of"),
                    "seconds": d.get("seconds"), "order": d.get("order", 99)})
    return sorted(out, key=lambda d: (d["order"], d["question"]))


def load(name: str, folder: Path | None = None) -> dict | None:
    folder = folder or get_settings().demo_dir
    path = folder / f"{slug(name)}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def replay(demo: dict, speed: float = 1.0, max_gap: float = 3.0) -> Iterator[tuple[float, dict]]:
    """(delay before the event, event): the recorded timing, with long waits shortened to max_gap seconds."""
    last = None
    for event in demo["events"]:
        t = event.get("t", 0.0)
        delay = 0.0 if last is None else min(max(t - last, 0.0), max_gap) / speed
        last = t
        yield delay, {**event, "replay": True}


def record(question: str, folder: Path | None = None, order: int = 99) -> Path:
    from .graph import Agent

    folder = folder or get_settings().demo_dir
    agent = Agent()
    started = time.time()
    events = []
    run_id, stream = agent.start(question, use_defaults=True)
    for event in stream:
        events.append({**event, "t": round(time.time() - started, 2)})
    final = events[-1]
    if final.get("type") != "final" or final.get("status") != "done":
        raise RuntimeError(f"Run did not finish: {final.get('status')} {final.get('message')}")
    demo = {"question": question, "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "data_size": final["data_size"], "data_as_of": final["data_as_of"],
            "seconds": round(time.time() - started, 1), "order": order, "events": events}
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{slug(question)}.json"
    path.write_text(json.dumps(demo, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="pantree-demo")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="run a question for real and save it as a demo")
    rec.add_argument("question", nargs="+")
    rec.add_argument("--order", type=int, default=99, help="position on the start screen")
    sub.add_parser("list")
    args = parser.parse_args()
    if args.command == "record":
        path = record(" ".join(args.question), order=args.order)
        print(f"Saved {path}")
    else:
        for d in demos():
            print(f"{d['order']:>3}  {d['question']}  "
                  f"({d['data_size']}, data as of {d['data_as_of']}, {d['seconds']}s)")


if __name__ == "__main__":
    main()
