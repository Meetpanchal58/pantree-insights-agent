"""Local preparation benchmark in fresh processes; no provider calls or hosting changes.

DATA_SIZE=small uv run python evals/startup_baseline.py
Fresh processes do not clear OS/filesystem caches. This is not Cloud Run or full-answer latency.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def worker(mode: str) -> dict:
    os.environ["CHECKPOINT_BACKEND"] = "memory"
    started = time.perf_counter()
    from pantree_insights import profile
    from pantree_insights.graph import Agent
    from pantree_insights.llm import FakeLLM
    from pantree_insights.memory import Memory

    imports = time.perf_counter() - started

    def no_model(*_):
        raise AssertionError("This benchmark must not call a model")

    with tempfile.TemporaryDirectory(prefix="pantree-startup-") as directory:
        root = Path(directory)
        # Isolate caches in both modes; existing published artifacts remain read-only.
        profile.CACHE = root / "profiles"
        if mode == "rebuild":
            profile.published_path = lambda info: root / "missing-published-profile"
        started = time.perf_counter()
        agent = Agent(llm_factory=lambda: FakeLLM(no_model), memory=Memory(root / "memory"))
        preparation = time.perf_counter() - started
        try:
            timings = []
            for _ in range(2):
                started = time.perf_counter()
                _, stream = agent.start("Show revenue by category")
                result = list(stream)[-1]
                assert result.get("kind") == "confirm", result
                timings.append(time.perf_counter() - started)
            return {
                "mode": mode,
                "data_size": agent.data["size"],
                "snapshot": agent.snapshot,
                "imports_seconds": imports,
                "preparation_seconds": preparation,
                "first_proposal_seconds": timings[0],
                "next_proposal_seconds": timings[1],
            }
        finally:
            agent.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["published", "rebuild"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker)))
        return
    for mode in ("published", "rebuild"):
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", mode], check=True)
    print(
        "Local data, memory checkpoints, no LLM, no full answers. Published mode may fall back if artifacts are stale."
    )


if __name__ == "__main__":
    main()
