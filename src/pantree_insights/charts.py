"""Pick and build a chart from the result's shape (no LLM): Plotly figure JSON for the browser and exports.

    a time column            -> line chart (one line per metric, or per category if there is one)
    one category + numbers   -> bar chart (horizontal when there are many or long labels)
    two categories + number  -> grouped bars
    two numbers, many rows   -> scatter
    one row of numbers       -> KPI cards (no chart)
"""
from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd
import plotly.graph_objects as go

PALETTE = ["#4F46E5", "#0EA5E9", "#F59E0B", "#10B981", "#EF4444", "#8B5CF6", "#64748B"]
VOLUME = re.compile(r"(^|_)(orders?|lines?|sessions|count|n|units|customers|buyers|tickets|reviews|impressions|"
                    r"contacts|visits|shipped_lines|delivered_lines)(_|$)")
RATIO = re.compile(r"(rate|share|pct|percent|ctr|conversion|margin|ratio|lift|growth|change)")
MONEY = re.compile(r"(revenue|sales|spend|aov|value|amount|cost|cac|price|gmv|profit|commission|refund)")
TIME = re.compile(r"^(day|date|week|month|quarter|year|period|.*_(date|day|week|month|at))$")


def _is_time(frame: pd.DataFrame, col: str) -> bool:
    s = frame[col]
    if pd.api.types.is_datetime64_any_dtype(s):
        return True
    textual = s.dtype == object or pd.api.types.is_string_dtype(s)        # pandas 3 has a str dtype
    return bool(TIME.match(col)) and textual and bool(pd.to_datetime(s, errors="coerce").notna().all())


def _fmt(col: str, series: pd.Series) -> dict[str, str]:
    """Axis format for a numeric column."""
    if RATIO.search(col) and series.dropna().abs().max() <= 1.5:
        return {"tickformat": ".1%"}
    if MONEY.search(col) and not VOLUME.search(col):
        return {"tickprefix": "₹", "tickformat": "~s"}
    return {"tickformat": "~s"}


def _label(col: str) -> str:
    return col.replace("_", " ").strip().capitalize()


def choose_columns(frame: pd.DataFrame) -> tuple[list[str], list[str], list[str]]:
    """(time columns, category columns, numeric columns), ignoring id-like columns."""
    numeric = [c for c in frame.columns if pd.api.types.is_numeric_dtype(frame[c]) and frame[c].dtype != bool]
    times = [c for c in frame.columns if _is_time(frame, c)]
    # ids and small integer periods (month 1-12, year, position) are labels, not values
    labels = [c for c in numeric if c.endswith("_id") or c in ("year", "position") or
              (TIME.match(c) and pd.api.types.is_integer_dtype(frame[c]) and frame[c].nunique() == len(frame))]
    numeric = [c for c in numeric if c not in times and c not in labels]
    cats = [c for c in frame.columns if c not in numeric and c not in times]   # includes the labels above
    named = [c for c in cats if not c.endswith("_id")]
    if named:                                    # ids only when there is nothing more readable
        cats = named
    # A category with one value per row is the label; any other category then only colours it.
    unique = [c for c in cats if frame[c].nunique() == len(frame)]
    if unique and len(cats) > 1:
        cats = [unique[0]]                       # the first such column is normally the name
    return times, cats, numeric


def main_metric(numeric: list[str]) -> str | None:
    """The column to plot: the first non-volume numeric column, else the first numeric."""
    non_volume = [c for c in numeric if not VOLUME.search(c)]
    return (non_volume or numeric or [None])[0]


def _series(numeric: list[str], metric: str) -> list[str]:
    """Columns that are the same measure for different periods/segments: realized_revenue_2025, _2026, ..."""
    stem = re.sub(r"_(\d{4}|current|previous|this_year|last_year|before|after|base)$", "", metric)
    if stem == metric:
        return []
    return [c for c in numeric if re.sub(r"_(\d{4}|current|previous|this_year|last_year|before|after|base)$", "", c)
            == stem and c != stem]


def build_chart(frame: pd.DataFrame, title: str = "", subtitle: str = "",
                metric: str | None = None) -> dict[str, Any] | None:
    """metric: the column to plot (the analyst's choice); falls back to a guess if missing or invalid."""
    if frame is None or frame.empty:
        return None
    frame = frame.copy()
    times, cats, numeric = choose_columns(frame)
    if times and cats and frame[cats[0]].nunique() == len(frame) and len(frame) <= 12:
        times = []                               # e.g. two named events: compare them as bars
    if not numeric:
        return None
    if len(frame) == 1 and not times:
        return {"kind": "kpi", "title": title,
                "values": [{"label": _label(c), "value": float(frame[c].iloc[0]), "format": _fmt(c, frame[c])}
                           for c in numeric[:4]]}

    metric = metric if metric in numeric else main_metric(numeric)
    fig = go.Figure()
    kind = "bar"
    if times:
        kind = "line"
        x = times[0]
        frame[x] = pd.to_datetime(frame[x])
        frame = frame.sort_values(x)
        if cats:
            for i, (name, part) in enumerate(frame.groupby(cats[0], sort=False)):
                fig.add_trace(go.Scatter(x=part[x], y=part[metric], name=str(name), mode="lines+markers",
                                         line={"color": PALETTE[i % len(PALETTE)], "width": 2}))
        else:
            lines = list(dict.fromkeys([metric] + [c for c in numeric if not VOLUME.search(c)]))[:3]
            for i, col in enumerate(lines):
                fig.add_trace(go.Scatter(x=frame[x], y=frame[col], name=_label(col), mode="lines+markers",
                                         line={"color": PALETTE[i % len(PALETTE)], "width": 2}))
            metric = lines[0]
        fig.update_xaxes(title=None)
    elif len(cats) >= 2 and frame[cats].isna().any().any():
        # several breakdowns stacked in one table (empty cells where a breakdown does not apply):
        # one bar per row, labelled by its filled-in values, coloured by the first column (the group)
        kind = "bar_h"
        group, rest = cats[0], cats[1:]
        frame["_label"] = frame[rest].apply(lambda r: " · ".join(str(v) for v in r if pd.notna(v)), axis=1)
        frame["_label"] = frame["_label"].where(frame["_label"] != "", frame[group].astype(str))
        for i, (name, part) in enumerate(frame.groupby(group, sort=False)):
            fig.add_trace(go.Bar(x=part[metric], y=part["_label"], orientation="h", name=str(name),
                                 marker_color=PALETTE[i % len(PALETTE)]))
        fig.update_yaxes(autorange="reversed", title=None, type="category")
        fig.update_layout(height=max(420, 26 * len(frame) + 120))
    elif len(cats) >= 2:
        kind = "grouped_bar"
        a, b = cats[0], cats[1]
        for i, (name, part) in enumerate(frame.groupby(b, sort=False)):
            fig.add_trace(go.Bar(x=part[a].astype(str), y=part[metric], name=str(name),
                                 marker_color=PALETTE[i % len(PALETTE)]))
        fig.update_layout(barmode="group")
        fig.update_xaxes(type="category")
    elif cats and len(series := _series(numeric, metric)) >= 2:
        kind = "grouped_bar"                     # e.g. revenue_2025 vs revenue_2026 per month
        labels = frame[cats[0]].astype(str)
        for i, col in enumerate(series):
            fig.add_trace(go.Bar(x=labels, y=frame[col], name=_label(col), marker_color=PALETTE[i % len(PALETTE)]))
        fig.update_layout(barmode="group")
        fig.update_xaxes(title=_label(cats[0]), type="category")
    elif cats:
        cat = cats[0]
        labels = frame[cat].astype(str)
        horizontal = len(frame) > 6 or labels.str.len().max() > 14
        extra = [c for c in numeric if c != metric][:2]
        hover = "<br>".join(f"{_label(c)}: %{{customdata[{i}]:,.4~g}}" for i, c in enumerate(extra))
        bar = go.Bar(x=frame[metric] if horizontal else labels, y=labels if horizontal else frame[metric],
                     orientation="h" if horizontal else "v", marker_color=PALETTE[0],
                     customdata=frame[extra].to_numpy() if extra else None,
                     hovertemplate=f"%{{{'y' if horizontal else 'x'}}}<br>{_label(metric)}: "
                                   f"%{{{'x' if horizontal else 'y'}:,.4~g}}" + (f"<br>{hover}" if hover else "")
                                   + "<extra></extra>")
        fig.add_trace(bar)
        # labels like "Diwali 2025" must not be read as dates: force a category axis
        if horizontal:
            fig.update_yaxes(autorange="reversed", title=None, type="category")
            kind = "bar_h"
        else:
            fig.update_xaxes(type="category")
    elif len(numeric) >= 2:
        kind = "scatter"
        fig.add_trace(go.Scatter(x=frame[numeric[0]], y=frame[numeric[1]], mode="markers",
                                 marker={"color": PALETTE[0], "size": 9}))
        fig.update_xaxes(title=_label(numeric[0]), **_fmt(numeric[0], frame[numeric[0]]))
        metric = numeric[1]
    else:
        return None

    value_axis = fig.update_xaxes if kind == "bar_h" else fig.update_yaxes
    value_axis(title=_label(metric), **_fmt(metric, frame[metric]))
    if fig.layout.height is None:
        fig.update_layout(height=420)
    full_title = title + (f"<br><sup>{subtitle}</sup>" if subtitle else "")
    fig.update_layout(title={"text": full_title, "x": 0, "xanchor": "left"}, template="plotly_white",
                      margin={"l": 60, "r": 20, "t": 70 if subtitle else 50, "b": 50},
                      legend={"orientation": "h", "y": -0.18}, font={"family": "Inter, system-ui, sans-serif"},
                      showlegend=len(fig.data) > 1)
    return {"kind": kind, "title": title, "metric": metric, "figure": json.loads(fig.to_json())}
