from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from app.categories import ISSUE_CATEGORIES
from app.config import settings


SYSTEM_PROMPT = """
You are a customer-support triage classifier for BeastBoard.
You must classify each message into exactly one category from this list:
- order_tracking
- delivery_delays
- refund_requests
- product_complaints
- subscription_issues
- payment_failures
- general_product_questions

Return strict JSON with this shape:
{
  "category": "one_of_allowed_categories",
  "confidence": 0.0_to_1.0,
  "reason": "short explanation",
  "suggested_action": "practical automation action"
}
""".strip()


KEYWORD_FALLBACK = {
    "order_tracking": ["where is my order", "track", "tracking", "shipment status"],
    "delivery_delays": ["late", "delay", "delayed", "not delivered", "delivery issue"],
    "refund_requests": ["refund", "money back", "return", "cancel and refund"],
    "product_complaints": ["damaged", "broken", "quality", "defect", "complaint"],
    "subscription_issues": ["subscription", "renewal", "plan", "cancel subscription"],
    "payment_failures": ["payment failed", "card declined", "upi failed", "checkout failed"],
}


def _safe_parse_json(text: str) -> dict[str, Any] | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    category = result.get("category", "general_product_questions")
    if category not in ISSUE_CATEGORIES:
        category = "general_product_questions"

    confidence = float(result.get("confidence", 0.6))
    confidence = max(0.0, min(1.0, confidence))

    reason = str(result.get("reason", "Classified using fallback logic."))
    suggested_action = str(
        result.get(
            "suggested_action",
            "Route to FAQ bot first, then escalate unresolved messages to human support.",
        )
    )

    return {
        "category": category,
        "confidence": confidence,
        "reason": reason,
        "suggested_action": suggested_action,
    }


def _fallback_classifier(message: str) -> dict[str, Any]:
    msg = message.lower()
    for category, keywords in KEYWORD_FALLBACK.items():
        if any(k in msg for k in keywords):
            return {
                "category": category,
                "confidence": 0.55,
                "reason": "Matched category-specific keywords in the message.",
                "suggested_action": "Automate first-response template and trigger category workflow.",
            }

    return {
        "category": "general_product_questions",
        "confidence": 0.5,
        "reason": "No high-confidence keyword match, treated as general inquiry.",
        "suggested_action": "Answer with product knowledge base and offer agent escalation.",
    }


def classify_customer_message(message: str) -> dict[str, Any]:
    if not settings.openai_api_key:
        return _fallback_classifier(message)

    client = OpenAI(api_key=settings.openai_api_key)
    completion = client.chat.completions.create(
        model=settings.openai_model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
    )

    content = completion.choices[0].message.content
    parsed = _safe_parse_json(content or "")
    if not parsed:
        return _fallback_classifier(message)

    return _normalize_result(parsed)
