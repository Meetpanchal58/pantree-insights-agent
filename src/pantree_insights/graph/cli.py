"""Graph: cli."""
from __future__ import annotations

from .agent import Agent


def main() -> None:
    """Ask and confirm an analytical plan from the terminal."""
    import argparse

    parser = argparse.ArgumentParser(prog="pantree-ask")
    parser.add_argument("question", nargs="+")
    args = parser.parse_args()
    question = " ".join(args.question)
    agent = Agent()
    print(f"\n{question}\n")
    run_id, events = agent.start(question)
    while True:
        final = None
        for event in events:
            if event.get("type") == "step" and event["status"] != "started":
                print(f"  · {event['step']} {event['status']} {event.get('detail', {}).get('seconds', '')}s")
            if event["type"] in ("clarify", "final"):
                final = event
        if final and final["type"] == "clarify":
            print("\n" + final["message"])
            events = agent.resume(run_id, input("You: ").strip(), final["plan_version"])
            continue
        break
    if final["status"] != "done":
        print(f"\n{final['status'].upper()}: {final.get('message')}")
        return
    a = final["analysis"]
    print(f"\n{a['headline']}\n" + "".join(f"  • {f}\n" for f in a["findings"]))
    print(f"SQL (verified):\n{final['sql']}")
