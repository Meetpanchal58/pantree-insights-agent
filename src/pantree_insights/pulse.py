"""The pulse: daily KPI history and change detection, with no LLM.

    pantree-pulse            # rebuild the daily KPI history and update memory/gaps

1. For every KPI in memory/kpis/kpi_definitions.yaml, compute numerator and denominator per day,
   for the total and each breakdown -> memory/kpis/daily_kpis.parquet (rebuilt from the data, not in git).
2. For each series, compare the latest 7 days (ending when the KPI has matured) with the 8 weeks before.
   A change is flagged when it is both unusual (z-score) and large (relative change).
3. Flags during a sale event that move in the expected direction are recorded as explained; others
   become open gaps in memory/gaps/open.json. Open gaps that are back to normal move to resolved.json.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from .config import get_settings
from .db import connect, manifest
from .semantic import Metrics, load_metrics

# Changes that sale events explain: rises while an event is live, and festival backlogs right after one.
UP_DURING_EVENTS = {"net_sales", "orders", "sessions", "conversion_rate", "marketing_spend", "platform_roas",
                    "avg_discount_pct", "support_tickets", "lost_sales_units"}
UP_AFTER_EVENTS = {"support_tickets", "late_delivery_rate", "cancellation_rate"}
DOWN_DURING_EVENTS = {"aov", "gross_margin_pct"}      # deeper discounts
AFTER_EVENT_DAYS = 14


# ----------------------------------------------------------------------------- definitions
@dataclass
class Settings:
    window_days: int = 7
    baseline_windows: int = 8
    min_z: float = 2.5             # legacy method only
    min_change: float = 0.15
    min_denominator: int = 50
    method: str = "robust_bh"      # robust_bh (median/MAD z + Benjamini-Hochberg) or legacy (mean/SD z)
    fdr_q: float = 0.10            # Benjamini-Hochberg false discovery rate across all series tested in a run


def load_definitions(memory_dir: Path) -> tuple[Settings, dict[str, dict]]:
    doc = yaml.safe_load((memory_dir / "kpis" / "kpi_definitions.yaml").read_text())
    return Settings(**doc.get("defaults", {})), doc["kpis"]


# ----------------------------------------------------------------------------- daily history
def daily_kpis(con: duckdb.DuckDBPyConnection, metrics: Metrics, kpis: dict[str, dict]) -> pd.DataFrame:
    """One row per day x KPI x breakdown segment, with numerator and denominator (so windows add up)."""
    by_source: dict[str, list[str]] = {}
    for name in kpis:
        by_source.setdefault(metrics.metrics[name].source, []).append(name)
    frames = []
    for source_name, names in by_source.items():
        source = metrics.sources[source_name]
        dims = sorted({d for n in names for d in kpis[n].get("breakdowns", [])})
        for dim in [None, *dims]:
            members = [n for n in names if dim is None or dim in kpis[n].get("breakdowns", [])]
            if not members:
                continue
            seg = source.dimensions[dim] if dim else "'all'"
            cols = []
            for n in members:
                m = metrics.metrics[n]
                cols.append(f"{m.numerator} AS \"{n}__num\"")
                cols.append(f"{m.denominator or 'NULL'} AS \"{n}__den\"")
            where = f"WHERE {source.where}" if source.where else ""
            sql = (f"SELECT CAST({source.time_column} AS DATE) AS day, CAST({seg} AS VARCHAR) AS segment, "
                   f"{', '.join(cols)} FROM {source.from_} {where} GROUP BY 1, 2")
            wide = con.execute(sql).df()
            for n in members:
                part = wide[["day", "segment", f"{n}__num", f"{n}__den"]].rename(
                    columns={f"{n}__num": "numerator", f"{n}__den": "denominator"})
                part.insert(1, "kpi", n)
                part.insert(2, "breakdown", dim or "total")
                frames.append(part)
    out = pd.concat(frames, ignore_index=True)
    out["day"] = pd.to_datetime(out["day"])
    out["numerator"] = out["numerator"].astype(float)
    out["denominator"] = out["denominator"].astype(float)
    return out.dropna(subset=["segment"]).sort_values(["kpi", "breakdown", "segment", "day"], ignore_index=True)


# ----------------------------------------------------------------------------- change detection
@dataclass
class Flag:
    id: str
    kpi: str
    label: str
    breakdown: str
    segment: str
    window_start: str
    window_end: str
    value: float
    baseline: float
    change_pct: float
    z: float
    direction: str                 # better / worse / up / down
    last_year: float | None
    sale_events: list[str]
    explained: str | None          # reason when the change is expected (e.g. a sale event)
    data_size: str
    data_as_of: str


def _value(num: float, den: float | None, is_ratio: bool) -> float | None:
    if is_ratio:
        return num / den if den else None
    return num


def robust_z(current: float, base: list[float]) -> float:
    """(current - median) / (1.4826 * MAD): a z-score that one unusual baseline week cannot inflate.
    Falls back to the standard deviation when MAD is 0 (flat baseline)."""
    med = float(np.median(base))
    mad = float(np.median(np.abs(np.array(base) - med))) * 1.4826
    if mad == 0:
        sd = float(np.std(base, ddof=1)) if len(base) > 1 else 0.0
        return (current - med) / sd if sd > 0 else (float("inf") if current != med else 0.0)
    return (current - med) / mad


def benjamini_hochberg(pvalues: list[float], q: float) -> list[bool]:
    """Which hypotheses are rejected at false discovery rate q (step-up procedure)."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    cutoff = 0
    for rank, i in enumerate(order, start=1):
        if pvalues[i] <= q * rank / m:
            cutoff = rank
    keep = set(order[:cutoff])
    return [i in keep for i in range(m)]


def detect(history: pd.DataFrame, metrics: Metrics, kpis: dict[str, dict], cfg: Settings, last_day: date,
           events: pd.DataFrame, data_size: str) -> list[Flag]:
    """Compare each series' latest 7-day window with the 8 windows before it.

    Windows are whole weeks (each weekday once), so day-of-week patterns cancel out. Method robust_bh: robust
    z (median/MAD) -> two-sided normal p-value -> Benjamini-Hochberg across all series tested; legacy: mean/SD
    z >= min_z. Both also require |change| >= min_change."""
    from math import erfc, sqrt

    w = cfg.window_days
    tested = []                                      # (kpi, breakdown, segment, current, centre, z, change, end)
    for (kpi, breakdown, segment), series in history.groupby(["kpi", "breakdown", "segment"], sort=False):
        metric = metrics.metrics[kpi]
        is_ratio = metric.denominator is not None
        end = pd.Timestamp(last_day - timedelta(days=kpis[kpi].get("mature_after_days", 0)))
        daily = series.set_index("day")[["numerator", "denominator"]]
        daily = daily.reindex(pd.date_range(end - timedelta(days=w * (cfg.baseline_windows + 1) - 1), end),
                              fill_value=0.0)

        def window(i: int, daily: pd.DataFrame = daily) -> tuple[float, float]:   # i = 0: the latest window
            block = daily.iloc[len(daily) - w * (i + 1): len(daily) - w * i]
            return block["numerator"].sum(), block["denominator"].sum()

        num, den = window(0)
        if is_ratio and den < cfg.min_denominator:
            continue
        current = _value(num, den, is_ratio)
        base = [_value(*window(i), is_ratio) for i in range(1, cfg.baseline_windows + 1)]
        base = [b for b in base if b is not None]
        if current is None or len(base) < cfg.baseline_windows // 2:
            continue
        if cfg.method == "legacy":
            centre = sum(base) / len(base)
            std = (sum((b - centre) ** 2 for b in base) / max(len(base) - 1, 1)) ** 0.5
            z = (current - centre) / std if std > 0 else float("inf")
        else:
            centre = float(np.median(base))
            z = robust_z(current, base)
        if centre == 0:
            continue
        tested.append((kpi, breakdown, segment, current, centre, z, current / centre - 1, end, is_ratio))

    if cfg.method == "legacy":
        chosen = [abs(t[5]) >= cfg.min_z for t in tested]
    else:
        pvalues = [erfc(abs(t[5]) / sqrt(2)) if t[5] != float("inf") else 0.0 for t in tested]
        chosen = benjamini_hochberg(pvalues, cfg.fdr_q)

    flags = []
    for (kpi, breakdown, segment, current, centre, z, change, end, is_ratio), keep in zip(tested, chosen, strict=True):
        if not keep or abs(change) < cfg.min_change:
            continue
        metric = metrics.metrics[kpi]
        start = end - timedelta(days=w - 1)
        names = events[(events.start_date <= end) & (events.end_date >= start)].name.tolist()
        recent = events[(events.end_date < start)
                        & (events.end_date >= start - timedelta(days=AFTER_EVENT_DAYS))].name.tolist()
        up = change > 0
        if metric.higher_is_better is None:
            direction = "up" if up else "down"
        else:
            direction = "better" if up == metric.higher_is_better else "worse"
        explained = None
        if names and up and kpi in UP_DURING_EVENTS:
            explained = f"expected rise during {', '.join(names)}"
        elif names and not up and kpi in DOWN_DURING_EVENTS:
            explained = f"deeper discounts during {', '.join(names)}"
        elif (names or recent) and up and kpi in UP_AFTER_EVENTS:
            explained = f"festival backlog around {', '.join(names or recent)}"

        ly = history[(history.kpi == kpi) & (history.breakdown == breakdown) & (history.segment == segment)
                     & (history.day >= start - timedelta(days=364)) & (history.day <= end - timedelta(days=364))]
        last_year = _value(ly.numerator.sum(), ly.denominator.sum(), is_ratio) if len(ly) else None

        flags.append(Flag(
            id=f"{kpi}|{breakdown}|{segment}", kpi=kpi, label=metric.label, breakdown=breakdown, segment=segment,
            window_start=str(start.date()), window_end=str(end.date()), value=round(current, 6),
            baseline=round(centre, 6), change_pct=round(change * 100, 1),
            z=round(z, 2) if z != float("inf") else 99.0,
            direction=direction, last_year=round(last_year, 6) if last_year is not None else None,
            sale_events=names, explained=explained, data_size=data_size, data_as_of=str(last_day)))
    return sorted(flags, key=lambda f: -abs(f.z))


# ----------------------------------------------------------------------------- gaps in memory
def _read_json(path: Path) -> list[dict]:
    return json.loads(path.read_text()) if path.exists() else []


def _write_json(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")


def update_gaps(memory_dir: Path, flags: list[Flag], data_size: str, as_of: str) -> dict[str, int]:
    """Open unexplained flags as gaps, refresh existing ones, resolve those back to normal."""
    open_path, resolved_path = memory_dir / "gaps" / "open.json", memory_dir / "gaps" / "resolved.json"
    open_gaps = _read_json(open_path)
    resolved = _read_json(resolved_path)
    current = {f.id: f for f in flags if f.explained is None}
    kept, opened, closed = [], 0, 0
    others = [g for g in open_gaps if g.get("kind") != "anomaly" or g.get("data_size") != data_size]
    for gap in open_gaps:
        if gap.get("kind") != "anomaly" or gap.get("data_size") != data_size:
            continue
        if gap["id"] in current:
            gap.update(asdict(current.pop(gap["id"])), kind="anomaly", status="open",
                       first_seen=gap.get("first_seen", as_of), last_seen=as_of)
            kept.append(gap)
        else:
            gap.update(status="resolved", resolved_on=as_of, resolution="back within the normal range")
            resolved.append(gap)
            closed += 1
    for flag in current.values():
        kept.append({**asdict(flag), "kind": "anomaly", "status": "open", "first_seen": as_of,
                     "last_seen": as_of, "note": None})
        opened += 1
    _write_json(open_path, others + kept)
    _write_json(resolved_path, resolved)
    return {"open": len(kept), "opened": opened, "resolved": closed}


# ----------------------------------------------------------------------------- run
def run(shop_dir: Path | None = None, memory_dir: Path | None = None) -> dict:
    settings = get_settings()
    shop_dir = shop_dir or settings.shop_dir
    memory_dir = memory_dir or settings.memory_dir
    info = manifest(shop_dir)
    last_day = date.fromisoformat(info["last_day"])
    metrics = load_metrics()
    cfg, kpis = load_definitions(memory_dir)
    con = connect(shop_dir)
    history = daily_kpis(con, metrics, kpis)
    events = con.execute("SELECT name, CAST(start_date AS DATE) AS start_date, CAST(end_date AS DATE) AS end_date "
                         "FROM sale_events").df()
    events["start_date"] = pd.to_datetime(events.start_date)
    events["end_date"] = pd.to_datetime(events.end_date)
    flags = detect(history, metrics, kpis, cfg, last_day, events, info["size"])

    kpi_dir = memory_dir / "kpis"
    kpi_dir.mkdir(parents=True, exist_ok=True)
    history.assign(data_size=info["size"]).to_parquet(kpi_dir / "daily_kpis.parquet", index=False)
    report = {"data_size": info["size"], "data_as_of": info["last_day"], "series": int(
        history.groupby(["kpi", "breakdown", "segment"]).ngroups), "flags": [asdict(f) for f in flags]}
    (kpi_dir / "latest_pulse.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    counts = update_gaps(memory_dir, flags, info["size"], info["last_day"])
    return {**{k: v for k, v in report.items() if k != "flags"}, "flags": len(flags),
            "explained": sum(f.explained is not None for f in flags), **counts}


def main() -> None:
    summary = run()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
