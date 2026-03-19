from __future__ import annotations

import asyncio
import json
from typing import Any

from openai import AsyncOpenAI

from app.config import settings


def _metric_manifest(metrics: dict) -> list[dict]:
    return [
        {"label": "Total Queries (30d)", "value": metrics.get("total_queries_30d", 0), "has_graph": True},
        {"label": "Queries Today", "value": metrics.get("queries_today", 0), "has_graph": True},
        {"label": "Queries Last 7d", "value": metrics.get("queries_last_7d", 0), "has_graph": True},
        {"label": "Unique Customers", "value": metrics.get("unique_customers_30d", 0), "has_graph": False},
        {"label": "Active Days", "value": metrics.get("active_days_30d", 0), "has_graph": False},
        {
            "label": "Avg Queries / Active Day",
            "value": metrics.get("avg_queries_per_active_day", 0),
            "has_graph": False,
        },
        {"label": "Avg Daily Queries (7d)", "value": metrics.get("avg_daily_queries_7d", 0), "has_graph": True},
        {
            "label": "Avg Classification Confidence",
            "value": metrics.get("avg_classification_confidence", 0),
            "has_graph": True,
        },
        {"label": "High Confidence Rate", "value": metrics.get("high_confidence_rate_pct", 0), "has_graph": True},
        {"label": "Top Issue Share", "value": metrics.get("top_issue_share_pct", 0), "has_graph": True},
        {"label": "Repeat Customer Rate", "value": metrics.get("repeat_customer_rate_pct", 0), "has_graph": True},
        {"label": "WoW Growth", "value": metrics.get("week_over_week_growth_pct", 0), "has_graph": True},
        {"label": "MoM Growth", "value": metrics.get("month_over_month_growth_pct", 0), "has_graph": True},
    ]


def _build_metric_messages(metric: dict[str, Any], by_category: list[dict], trends: list[dict]) -> list[dict[str, str]]:
    expected_lines = 3 if metric.get("has_graph") else 1
    system = (
        "You are an analytics narrator for BeastBoard. "
        "Write concise metric explanations from the provided dashboard data."
    )
    user = (
        "Generate explanation for exactly ONE metric.\n"
        "Rules:\n"
        f"1) Output exactly {expected_lines} line(s).\n"
        "2) Keep each line short, data-grounded, and actionable.\n"
        "3) Never invent data.\n"
        "4) Return plain text only, no bullets or numbering.\n"
        "\n"
        "Metric JSON:\n"
        + json.dumps(metric)
        + "\n\n"
        "Category distribution JSON:\n"
        + json.dumps(by_category)
        + "\n\n"
        "Trend data JSON:\n"
        + json.dumps(trends)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _fallback_text_for_metric(metric: dict[str, Any]) -> str:
    label = metric["label"]
    value = metric["value"]
    if metric["has_graph"]:
        return "\n".join(
            [
                f"Current value is {value}, and the chart confirms the recent level.",
                "Watch directional change in the plotted series to detect momentum shifts early.",
                "Use this trend with category mix to prioritize automation and staffing decisions.",
            ]
        )
    return f"Current value is {value}, which should be monitored as a baseline operational KPI."


def fallback_metric_explanations(metrics: dict) -> dict[str, str]:
    manifest = _metric_manifest(metrics)
    output: dict[str, str] = {}
    for item in manifest:
        output[item["label"]] = _fallback_text_for_metric(item)
    return output


async def explain_metric(
    client: AsyncOpenAI,
    *,
    metric: dict[str, Any],
    by_category: list[dict],
    trends: list[dict],
) -> str:
    try:
        completion = await client.chat.completions.create(
            model=settings.openai_model,
            temperature=0.2,
            messages=_build_metric_messages(metric, by_category, trends),
        )
        text = (completion.choices[0].message.content or "").strip()
        if not text:
            return _fallback_text_for_metric(metric)

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        needed = 3 if metric.get("has_graph") else 1
        if len(lines) < needed:
            fallback = _fallback_text_for_metric(metric).splitlines()
            lines.extend(fallback[len(lines):needed])
        return "\n".join(lines[:needed])
    except Exception:
        return _fallback_text_for_metric(metric)


async def generate_metric_explanations_parallel(metrics: dict, by_category: list[dict], trends: list[dict]) -> dict[str, str]:
    manifest = _metric_manifest(metrics)
    if not settings.openai_api_key:
        return fallback_metric_explanations(metrics)

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    tasks = [
        explain_metric(
            client,
            metric=item,
            by_category=by_category,
            trends=trends,
        )
        for item in manifest
    ]
    results = await asyncio.gather(*tasks)
    return {item["label"]: results[idx] for idx, item in enumerate(manifest)}