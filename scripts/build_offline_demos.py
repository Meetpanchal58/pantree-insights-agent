"""Build four reproducible, model-free demo snapshots from the selected DuckDB dataset.

DATA_SIZE=small uv run python scripts/build_offline_demos.py
Commits both the readable SQL/results and a browser bundle; playback needs no API calls.
"""

from __future__ import annotations

import hashlib
import json

from pantree_insights.config import ROOT, get_settings
from pantree_insights.db import connect, manifest, run_query
from pantree_insights.guardrails import check_sql


def money(value):
    return f"Rs {value:,.0f}"


def number(value):
    return f"{value:,.0f}"


def change(new, old):
    return f"{(new / old - 1) * 100:+.1f}%" if old else "not comparable with a zero baseline"


def build():
    settings, info, con = get_settings(), manifest(), connect()
    start, end = "2026-01-01", info["last_day"]
    # Date bounds are explicit in these three questions, independent of moving wall-clock time.
    period = f"1 January 2026 to {end}"
    demos = []

    def save(slug, topic, question, sql, describe, chart_column, chart_filter=None):
        verdict = check_sql(sql)
        if not verdict.ok:
            raise ValueError(verdict.errors)
        result = run_query(con, verdict.sql, max_rows=500)
        if result.truncated:
            raise ValueError(f"{slug}: truncated demo")
        rows = json.loads(result.frame.to_json(orient="records", date_format="iso"))
        analysis = describe(rows)
        shown = [r for r in rows if chart_filter is None or chart_filter(r)]
        label = {
            "diwali-sales": "event_name",
            "brand-product-sales": "brand_name",
            "conversion-funnel": "stage",
            "marketing-roas": "channel",
        }[slug]
        chart = {
            "kind": "offline_bar",
            "title": analysis["chart_title"],
            "metric": chart_column,
            "labels": [r[label] for r in shown],
            "values": [r[chart_column] for r in shown],
        }
        final = {
            "type": "final",
            "status": "done",
            "question": question,
            "replay": True,
            "sql": verdict.sql,
            "result": {
                "columns": list(result.frame.columns),
                "data": json.loads(result.frame.to_json(orient="split", index=False, date_format="iso"))["data"],
            },
            "analysis": analysis,
            "chart": chart,
            "data_as_of": end,
            "data_size": info["size"],
            "attempts": 1,
            "warnings": [],
            "offline": True,
        }
        events = [
            {
                "type": "step",
                "step": "offline_demo",
                "status": "done",
                "detail": {
                    "understood": question,
                    "reason": "Saved SQL results and editorial insights; no live model was run.",
                },
            },
            final,
        ]
        demos.append(
            {
                "slug": slug,
                "topic": topic,
                "question": question,
                "order": len(demos) + 1,
                "data_size": info["size"],
                "data_as_of": end,
                "seconds": 0,
                "offline": True,
                "provenance": {"manifest": info, "sql_sha256": hashlib.sha256(verdict.sql.encode()).hexdigest()},
                "events": events,
            }
        )

    def diwali(rows):
        old, new = rows
        return {
            "headline": (
                "Diwali 2026 realized revenue per day changed "
                f"{change(new['revenue_per_day'], old['revenue_per_day'])} versus Diwali 2025."
            ),
            "findings": [
                (
                    f"2026 generated {money(new['realized_revenue'])} across "
                    f"{number(new['event_days'])} event days, versus "
                    f"{money(old['realized_revenue'])} across {number(old['event_days'])} days in "
                    "2025."
                ),
                (
                    f"Revenue per day was {money(new['revenue_per_day'])} versus "
                    f"{money(old['revenue_per_day'])}; daily order volume changed "
                    f"{change(new['orders_per_day'], old['orders_per_day'])}."
                ),
                (
                    f"Revenue per placed order was {money(new['revenue_per_order'])} versus "
                    f"{money(old['revenue_per_order'])}, separating basket realization from order "
                    "volume."
                ),
            ],
            "recommendations": [
                (
                    "Use per-day revenue and order volume when allocating the next festive "
                    "campaign budget; unequal event lengths distort total-only comparisons."
                ),
                "Review brand mix, discounting and fulfilment maturity before attributing the change to the campaign.",
            ],
            "caveats": [
                (
                    "Simulated data. Realized revenue includes delivered revenue less recorded "
                    "refunds; recent deliveries and refunds can change the comparison."
                ),
                (
                    "Revenue per placed order is not delivered-order AOV. This comparison does "
                    "not identify a causal campaign effect."
                ),
            ],
            "chart_title": "Diwali realized revenue per event day",
        }

    save(
        "diwali-sales",
        "Diwali sales comparison",
        (
            "How did Diwali 2026 compare with Diwali 2025 in realized revenue per day, daily "
            "orders and revenue per placed order?"
        ),
        """WITH totals AS (
           SELECT e.name AS event_name, e.start_date, e.end_date,
             date_diff('day', e.start_date, e.end_date) + 1 AS event_days,
             COUNT(DISTINCT s.order_id) AS orders,
             COALESCE(SUM(CASE WHEN s.delivered_at IS NOT NULL
               THEN s.net_amount - COALESCE(s.refund_amount, 0) ELSE 0 END), 0) AS realized_revenue
           FROM sale_events e LEFT JOIN sales s
             ON s.placed_at >= e.start_date AND s.placed_at < e.end_date + INTERVAL 1 DAY AND s.line_type = 'Sale'
           WHERE e.name IN ('Diwali 2025', 'Diwali 2026') GROUP BY 1, 2, 3
         ) SELECT event_name, start_date, end_date, event_days, orders, realized_revenue,
           realized_revenue / event_days AS revenue_per_day,
           orders * 1.0 / event_days AS orders_per_day,
           realized_revenue / NULLIF(orders, 0) AS revenue_per_order FROM totals ORDER BY start_date""",
        diwali,
        "revenue_per_day",
    )

    def brands(rows):
        brand = [r for r in rows if r["view"] == "Brand"]
        products = [r for r in rows if r["view"] == "Product"]
        top, product = brand[0], products[0]
        total = top["total_revenue"]
        share = sum(r["realized_revenue"] for r in brand) / total if total else 0
        return {
            "headline": (
                f"{top['brand_name']} leads brand revenue; the top five brands contribute "
                f"{share:.1%} of realized revenue."
            ),
            "findings": [
                (
                    f"{top['brand_name']} generated {money(top['realized_revenue'])} from "
                    f"{number(top['orders'])} distinct orders containing that brand."
                ),
                (
                    f"The leading product was {product['product_name']} "
                    f"({product['brand_name']}), with {money(product['realized_revenue'])} and "
                    f"{number(product['orders'])} orders."
                ),
                (
                    "The top five brands produced "
                    f"{money(sum(r['realized_revenue'] for r in brand))} of the shop's "
                    f"{money(total)} realized revenue in the period."
                ),
            ],
            "recommendations": [
                (
                    "Prioritize availability checks for the leading products, then assess margin "
                    "and return rates before increasing stock."
                ),
                (
                    "Monitor concentration in the leading brands and test alternative brands "
                    "rather than assuming revenue leaders are the most profitable."
                ),
            ],
            "caveats": [
                "Brand and product rankings are separate views; do not add their revenue or order counts together.",
                (
                    "One order can contain several brands/products. Exact stored brand spelling "
                    "is preserved; simulated revenue does not establish demand or profit."
                ),
            ],
            "chart_title": "Top five brands by realized revenue",
        }

    save(
        "brand-product-sales",
        "Brand and product sales",
        (
            "Which five brands and five products generated the most realized revenue from "
            f"{period}, and how concentrated were brand sales?"
        ),
        f"""WITH lines AS (
           SELECT p.brand_name, p.product_id, p.product_name, s.order_id,
             CASE WHEN s.delivered_at IS NOT NULL THEN s.net_amount - COALESCE(s.refund_amount, 0) ELSE 0 END AS revenue
           FROM sales s JOIN product_master p ON p.product_id = s.product_id
           WHERE s.line_type = 'Sale' AND s.placed_at >= TIMESTAMP '{start}'
             AND s.placed_at < DATE '{end}' + INTERVAL 1 DAY
         ), ranked AS (
           SELECT 'Brand' AS view, brand_name, NULL AS product_id, NULL AS product_name,
             SUM(revenue) AS realized_revenue, COUNT(DISTINCT order_id) AS orders,
             ROW_NUMBER() OVER (ORDER BY SUM(revenue) DESC, brand_name) AS position FROM lines GROUP BY brand_name
           UNION ALL
           SELECT 'Product', brand_name, product_id, product_name, SUM(revenue), COUNT(DISTINCT order_id),
             ROW_NUMBER() OVER (ORDER BY SUM(revenue) DESC, product_id)
           FROM lines GROUP BY brand_name, product_id, product_name
         ) SELECT view, brand_name, product_id, product_name, realized_revenue, orders, position,
           (SELECT SUM(revenue) FROM lines) AS total_revenue FROM ranked WHERE position <= 5 ORDER BY view, position""",
        brands,
        "realized_revenue",
        lambda r: r["view"] == "Brand",
    )

    def funnel(rows):
        drops = rows[1:]
        worst = max(drops, key=lambda r: r["lost_sessions"])
        purchase = rows[-1]
        return {
            "headline": (
                f"The largest measured funnel loss is before {worst['stage'].lower()}: "
                f"{number(worst['lost_sessions'])} sessions drop out at that step."
            ),
            "findings": [
                (
                    f"Of {number(rows[0]['sessions'])} observed sessions, "
                    f"{number(purchase['sessions'])} completed the measured sequence through "
                    f"purchase ({purchase['share_of_sessions']:.1%})."
                ),
                (
                    f"The {worst['stage'].lower()} step retained "
                    f"{worst['step_conversion_rate']:.1%} of sessions reaching the previous stage."
                ),
                (
                    f"Checkout completion was {purchase['step_conversion_rate']:.1%}; "
                    f"{number(purchase['lost_sessions'])} sessions reached checkout without a "
                    "subsequent measured purchase."
                ),
            ],
            "recommendations": [
                (
                    "Inspect device and traffic-source breakdowns at the "
                    f"{worst['stage'].lower()} step; prioritize the largest recoverable session "
                    "volume."
                ),
                (
                    "Investigate payment failures, shipping charges and checkout usability, then "
                    "validate changes with a controlled experiment."
                ),
            ],
            "caveats": [
                (
                    "This is a strict same-session sequence: product view → add to cart → "
                    "checkout → purchase, in timestamp order. It is not all purchase sessions "
                    "divided by all sessions."
                ),
                (
                    "Missing events and cross-session purchases can look like abandonment. The "
                    "funnel locates losses but does not prove their causes."
                ),
            ],
            "chart_title": "Sessions progressing through the measured conversion funnel",
        }

    save(
        "conversion-funnel",
        "Conversion funnel",
        (
            "Where do sessions drop out between product view, add to cart, checkout and purchase "
            f"from {period}, and which step should we investigate first?"
        ),
        f"""WITH events AS (
           SELECT session_id, event_type, event_at FROM clickstream
           WHERE event_at >= TIMESTAMP '{start}' AND event_at < DATE '{end}' + INTERVAL 1 DAY
         ), views AS (
           SELECT session_id, MIN(event_at) AS viewed_at FROM events WHERE event_type = 'pdp_view' GROUP BY 1
         ), carts AS (
           SELECT e.session_id, MIN(e.event_at) AS carted_at FROM events e JOIN views v USING(session_id)
           WHERE e.event_type = 'add_to_cart' AND e.event_at >= v.viewed_at GROUP BY 1
         ), checkouts AS (
           SELECT e.session_id, MIN(e.event_at) AS checked_at FROM events e JOIN carts c USING(session_id)
           WHERE e.event_type = 'checkout_start' AND e.event_at >= c.carted_at GROUP BY 1
         ), purchases AS (
           SELECT e.session_id FROM events e JOIN checkouts c USING(session_id)
           WHERE e.event_type = 'purchase' AND e.event_at >= c.checked_at GROUP BY 1
         ), counts AS (
           SELECT 1 AS position, 'Observed sessions' AS stage, COUNT(DISTINCT session_id) AS sessions FROM events
           UNION ALL SELECT 2, 'Product view', COUNT(*) FROM views
           UNION ALL SELECT 3, 'Add to cart', COUNT(*) FROM carts
           UNION ALL SELECT 4, 'Checkout', COUNT(*) FROM checkouts
           UNION ALL SELECT 5, 'Purchase', COUNT(*) FROM purchases
         ) SELECT position, stage, sessions,
           sessions * 1.0 / NULLIF(FIRST_VALUE(sessions) OVER (ORDER BY position), 0) AS share_of_sessions,
           sessions * 1.0 / NULLIF(LAG(sessions) OVER (ORDER BY position), 0) AS step_conversion_rate,
           COALESCE(LAG(sessions) OVER (ORDER BY position) - sessions, 0) AS lost_sessions
         FROM counts ORDER BY position""",
        funnel,
        "sessions",
    )

    def marketing(rows):
        paid = [r for r in rows if r["marketing_spend"] > 0 and r["last_click_roas"] is not None]
        top, largest = paid[0], max(paid, key=lambda r: r["marketing_spend"])
        spend, revenue = sum(r["marketing_spend"] for r in rows), sum(r["attributed_revenue"] for r in rows)
        return {
            "headline": (
                f"{top['channel']} has the highest last-click ROAS at "
                f"{top['last_click_roas']:.1f}×; {largest['channel']} receives the most spend."
            ),
            "findings": [
                (
                    f"Marketing spend totaled {money(spend)}, with {money(revenue)} in last-click "
                    f"attributed booked revenue ({revenue / spend:.1f}× blended ROAS)."
                ),
                (
                    f"{top['channel']} received {money(top['marketing_spend'])} and was credited "
                    f"{money(top['attributed_revenue'])}; high efficiency should be considered "
                    "alongside its spend scale."
                ),
                (
                    f"{largest['channel']} used {largest['marketing_spend'] / spend:.1%} of total "
                    f"spend and returned {largest['last_click_roas']:.1f}× attributed revenue per "
                    "rupee."
                ),
            ],
            "recommendations": [
                (
                    "Test small, measured budget changes across channels; monitor marginal ROAS "
                    "and incremental conversions as spend increases."
                ),
                (
                    "Check attribution coverage, audience overlap and fulfilled-order "
                    "profitability before moving substantial budget."
                ),
            ],
            "caveats": [
                (
                    "Last-click attribution assigns credit; it does not prove incremental "
                    "impact. Booked revenue is not reduced by later cancellations or refunds."
                ),
                (
                    "Spend and attributed orders use the same date window. Direct/organic orders "
                    "without campaign touches receive no channel credit; simulated ROAS is not "
                    "profit."
                ),
            ],
            "chart_title": "Last-click attributed revenue per rupee of marketing spend",
        }

    save(
        "marketing-roas",
        "Marketing spend and ROAS",
        (
            f"Which marketing channels deliver the strongest last-click ROAS from {period}, and "
            "how do their spend and attributed revenue compare?"
        ),
        f"""WITH spend AS (
           SELECT c.channel, SUM(m.spend) AS marketing_spend FROM marketing_spend m
           JOIN campaigns c ON c.campaign_id = m.campaign_id
           WHERE m.date_hour >= TIMESTAMP '{start}' AND m.date_hour < DATE '{end}' + INTERVAL 1 DAY GROUP BY 1
         ), revenue AS (
           SELECT c.channel, SUM(a.credit_last_click * a.order_revenue) AS attributed_revenue
           FROM marketing_attribution a JOIN campaigns c ON c.campaign_id = a.campaign_id
           WHERE a.order_at >= TIMESTAMP '{start}' AND a.order_at < DATE '{end}' + INTERVAL 1 DAY GROUP BY 1
         ) SELECT s.channel, s.marketing_spend, COALESCE(r.attributed_revenue, 0) AS attributed_revenue,
           COALESCE(r.attributed_revenue, 0) / NULLIF(s.marketing_spend, 0) AS last_click_roas
           FROM spend s LEFT JOIN revenue r USING(channel) ORDER BY last_click_roas DESC NULLS LAST, channel""",
        marketing,
        "last_click_roas",
    )
    con.close()
    folder = settings.demo_dir
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.json"):
        old.unlink()
    for demo in demos:
        (folder / f"{demo['slug']}.json").write_text(json.dumps(demo, indent=2, ensure_ascii=False) + "\n")
    bundle = ROOT / "src/pantree_insights/static/offline-demos.js"
    bundle.write_text(
        "// Generated by scripts/build_offline_demos.py; no model calls.\nwindow.PANTREE_DEMOS = "
        + json.dumps(demos, ensure_ascii=False, separators=(",", ":"))
        + ";\n"
    )
    print(f"Built {len(demos)} offline demos from {info['size']} data as of {end}.")


if __name__ == "__main__":
    build()
