"""Small dependency-free run telemetry.

Structured JSON logs are intentionally enough for Cloud Logging. They can later be
exported to OpenTelemetry without changing call sites.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("pantree.telemetry")


@dataclass
class RunTelemetry:
    run_id: str
    question: str
    started: float = field(default_factory=time.perf_counter)
    steps: dict[str, float] = field(default_factory=dict)

    def mark(self, step: str, seconds: float) -> None:
        self.steps[step] = round(seconds, 3)

    def finish(self, *, status: str, attempts: int = 0, cost_usd: float = 0.0,
               verification: str | None = None) -> None:
        payload = {
            "event": "analysis_run",
            "run_id": self.run_id,
            "status": status,
            "seconds": round(time.perf_counter() - self.started, 3),
            "attempts": attempts,
            "cost_usd": round(cost_usd, 6),
            "verification": verification,
            "step_seconds": self.steps,
        }
        log.info("%s", json.dumps(payload, sort_keys=True))
