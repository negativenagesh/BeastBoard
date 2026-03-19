from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
import re
import sys
import uvicorn

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.categories import ISSUE_CATEGORIES
from app.config import settings
from app.db import (
    count_messages,
    delete_message_by_id,
    fetch_distribution,
    fetch_metrics,
    fetch_recent_messages,
    fetch_trends,
    init_db,
    insert_message,
    message_exists_by_external_id,
    normalize_whatsapp_self_user_ids,
    purge_filtered_whatsapp_messages,
    purge_whatsapp_self_outlier_users,
)
from app.schemas import AnalyzeMessageRequest, AnalyzeMessageResponse, DashboardResponse, InsightsSummaryResponse, WhatsAppConnectResponse
from app.services.analytics import generate_automation_recommendations
from app.services.classifier import classify_customer_message
from app.services.explanations import generate_metric_explanations_parallel
from app.services.whatsapp import fetch_self_messages, session_status, start_session

logger = logging.getLogger("beastboard")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)

_sse_clients: set[asyncio.Queue] = set()


def _notify_dashboard_clients() -> None:
    """Push a signal to every connected SSE client."""
    for q in _sse_clients:
        try:
            q.put_nowait("dashboard_updated")
        except asyncio.QueueFull:
            pass
    logger.info("SSE: notified %d connected dashboard client(s)", len(_sse_clients))


POLL_INTERVAL_SECONDS = 10
_explanations_cache_lock = asyncio.Lock()
_explanations_cache: dict = {
    "fingerprint": "",
    "explanations": {},
}


def _normalize_trend_granularity(value: str | None) -> str:
    if value in {"day", "week", "month"}:
        return value
    return "day"


async def _poll_ultramsg_loop() -> None:
    """Background loop: polls UltraMSG /chats/messages for new self-messages."""
    logger.info("Background poller started (interval=%ds)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            result = await _sync_ultramsg_self_messages(limit=100)
            if result.get("ingested", 0) > 0:
                logger.info(
                    "Poller: %d NEW message(s) ingested, notifying dashboard",
                    result["ingested"],
                )
                _notify_dashboard_clients()
            else:
                logger.debug(
                    "Poller: no new messages (fetched=%d, duplicates=%d)",
                    result.get("fetched", 0), result.get("duplicates", 0),
                )
        except Exception:
            logger.exception("Poller: error during sync cycle")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    init_db()
    normalized_count = normalize_whatsapp_self_user_ids()
    if normalized_count > 0:
        logger.info("Normalized %d WhatsApp user_id value(s)", normalized_count)

    purged_count = purge_filtered_whatsapp_messages()
    if purged_count > 0:
        logger.info("Purged %d filtered WhatsApp rows from DB", purged_count)

    outlier_count = purge_whatsapp_self_outlier_users()
    if outlier_count > 0:
        logger.info("Purged %d outlier WhatsApp user row(s)", outlier_count)

    logger.info("Database initialized.")
    if not settings.whatsapp_gateway_url or not settings.whatsapp_gateway_token:
        logger.warning(
            "UltraMSG sync disabled: WHATSAPP_GATEWAY_URL and/or WHATSAPP_GATEWAY_TOKEN not loaded. "
            "Check .env loading and process working directory."
        )
    poll_task = asyncio.create_task(_poll_ultramsg_loop())
    logger.info("Background UltraMSG poller launched.")
    yield
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    logger.info("Background poller stopped.")


app = FastAPI(title="BeastBoard API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.get("/api/categories")
def get_categories() -> dict:
    return {"categories": ISSUE_CATEGORIES}


def _canonical_whatsapp_identity(value: str | None) -> str:
    if not value:
        return ""
    normalized = str(value).strip().lower().replace("+", "")
    local_part = normalized.split("@", 1)[0]
    digits = re.sub(r"\D", "", local_part)
    return digits or local_part


def _is_blocked_dashboard_message(text: str | None) -> bool:
    return "[clawdbot]" in (text or "").lower()


async def _sync_ultramsg_self_messages(limit: int = 100) -> dict:
    if not settings.whatsapp_gateway_url or not settings.whatsapp_gateway_token:
        return {
            "enabled": False,
            "fetched": 0,
            "ingested": 0,
            "duplicates": 0,
            "note": "Set WHATSAPP_GATEWAY_URL and WHATSAPP_GATEWAY_TOKEN to enable UltraMsg sync.",
        }

    fetched = 0
    ingested = 0
    duplicates = 0

    records = await fetch_self_messages(limit=limit)
    fetched = len(records)

    for item in records:
        if _is_blocked_dashboard_message(item.get("message")):
            duplicates += 1
            continue

        ext_id = item.get("external_message_id")
        if ext_id and message_exists_by_external_id("whatsapp_self", ext_id):
            duplicates += 1
            continue

        classification = classify_customer_message(item["message"])
        row = insert_message(
            source="whatsapp_self",
            user_id=item.get("user_id"),
            external_message_id=ext_id,
            raw_message=item["message"],
            category=classification["category"],
            confidence=classification["confidence"],
            reason=classification["reason"],
            suggested_action=classification["suggested_action"],
            created_at=item.get("created_at"),
        )
        if row.get("inserted"):
            ingested += 1
            logger.info(
                "Ingested message id=%s: %.50s...",
                row.get("id"), item["message"][:50],
            )
        else:
            duplicates += 1

    if fetched:
        logger.info(
            "Sync result: fetched=%d  ingested=%d  duplicates=%d",
            fetched, ingested, duplicates,
        )

    return {
        "enabled": True,
        "fetched": fetched,
        "ingested": ingested,
        "duplicates": duplicates,
    }


async def _safe_sync_ultramsg_self_messages(limit: int = 100) -> dict:
    try:
        return await _sync_ultramsg_self_messages(limit=limit)
    except Exception as exc:  # pragma: no cover - network dependent
        logger.warning("UltraMSG sync skipped due to transient error: %s", exc)
        return {
            "enabled": True,
            "fetched": 0,
            "ingested": 0,
            "duplicates": 0,
            "degraded": True,
            "note": f"UltraMSG sync temporarily unavailable: {exc}",
        }


@app.post("/api/messages/analyze", response_model=AnalyzeMessageResponse)
def analyze_message(payload: AnalyzeMessageRequest) -> AnalyzeMessageResponse:
    classification = classify_customer_message(payload.message)

    row = insert_message(
        source=payload.source,
        user_id=payload.user_id,
        raw_message=payload.message,
        category=classification["category"],
        confidence=classification["confidence"],
        reason=classification["reason"],
        suggested_action=classification["suggested_action"],
    )

    return AnalyzeMessageResponse(
        id=row["id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        source=row["source"],
        user_id=row["user_id"],
        message=row["message"],
        classification=classification,
    )


@app.post("/api/whatsapp/connect/start", response_model=WhatsAppConnectResponse)
async def whatsapp_connect_start() -> WhatsAppConnectResponse:
    try:
        return WhatsAppConnectResponse(**(await start_session()))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to start WhatsApp session: {exc}") from exc


@app.get("/api/whatsapp/connect/{session_id}", response_model=WhatsAppConnectResponse)
async def whatsapp_connect_status(session_id: str) -> WhatsAppConnectResponse:
    try:
        return WhatsAppConnectResponse(**(await session_status(session_id)))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch session status: {exc}") from exc


@app.post("/api/whatsapp/webhook")
async def whatsapp_webhook(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook payload must be a JSON object")

    logger.info("Webhook received payload: %s", payload)

    incoming: list[dict] = []

    if isinstance(payload.get("data"), dict):
        incoming = [payload["data"]]
        logger.info("Webhook shape: nested 'data' object")
    elif isinstance(payload.get("messages"), list):
        incoming = payload["messages"]
        logger.info("Webhook shape: 'messages' array (%d items)", len(incoming))
    elif isinstance(payload.get("messages"), dict):
        incoming = [payload["messages"]]
        logger.info("Webhook shape: 'messages' single dict")
    elif payload.get("from") or payload.get("body"):
        incoming = [payload]
        logger.info("Webhook shape: flat payload")
    else:
        logger.warning("Webhook: unrecognized payload structure, keys=%s", list(payload.keys()))

    processed = 0
    for msg in incoming:
        if not isinstance(msg, dict):
            continue

        msg_from = _canonical_whatsapp_identity(msg.get("from") or msg.get("chatId"))
        msg_to = _canonical_whatsapp_identity(msg.get("to") or msg.get("chatId"))

        logger.info(
            "Webhook message: from=%s  to=%s  body_preview=%.60s",
            msg_from, msg_to,
            (msg.get("body") or msg.get("text") or "")[:60],
        )

        if not msg_from or not msg_to:
            logger.info("Skipped: missing from/to")
            continue
        if msg_from != msg_to:
            logger.info("Skipped: from != to (not a self-message)")
            continue

        text = None
        if isinstance(msg.get("text"), dict):
            text = (msg.get("text") or {}).get("body")
        else:
            text = msg.get("text")
        if not text:
            text = msg.get("body")

        if not text:
            logger.info("Skipped: empty message body")
            continue

        if _is_blocked_dashboard_message(text):
            logger.info("Skipped: clawdbot-tagged message")
            continue

        user_id = msg_from
        classification = classify_customer_message(text)
        logger.info(
            "Classified as: category=%s  confidence=%.2f",
            classification["category"], classification["confidence"],
        )

        result = insert_message(
            source="whatsapp_self",
            user_id=user_id,
            external_message_id=str(msg.get("id")) if msg.get("id") is not None else None,
            raw_message=text,
            category=classification["category"],
            confidence=classification["confidence"],
            reason=classification["reason"],
            suggested_action=classification["suggested_action"],
        )
        inserted = result.get("inserted", False)
        logger.info(
            "Message DB result: id=%s  inserted=%s",
            result.get("id"), inserted,
        )
        if inserted:
            processed += 1

    if processed > 0:
        _notify_dashboard_clients()
        logger.info("Webhook: %d new message(s) ingested, SSE clients notified", processed)
    else:
        logger.info("Webhook: 0 new messages ingested (received=%d)", len(incoming))

    return {"received": len(incoming), "processed": processed}


@app.get("/api/dashboard/events")
async def dashboard_events(request: Request) -> StreamingResponse:
    """SSE endpoint: pushes 'dashboard_updated' events when new messages arrive."""

    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    _sse_clients.add(queue)
    logger.info("SSE client connected (total=%d)", len(_sse_clients))

    async def _event_stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"event: {event}\ndata: {{}}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            _sse_clients.discard(queue)
            logger.info("SSE client disconnected (remaining=%d)", len(_sse_clients))

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/insights/summary", response_model=InsightsSummaryResponse)
async def insights_summary(days: int = 30) -> InsightsSummaryResponse:
    await _safe_sync_ultramsg_self_messages(limit=100)
    total = count_messages(days)
    distribution = fetch_distribution(days)
    recommendations = generate_automation_recommendations(days)

    return InsightsSummaryResponse(
        total_messages=total,
        by_category=distribution,
        automation_recommendations=recommendations,
    )


@app.get("/api/insights/trends")
async def insights_trends(days: int = 14, granularity: str = "day") -> dict:
    await _safe_sync_ultramsg_self_messages(limit=100)
    normalized = _normalize_trend_granularity(granularity)
    return {
        "days": days,
        "granularity": normalized,
        "trends": fetch_trends(days, granularity=normalized),
    }


async def _build_dashboard_snapshot(days: int, trend_days: int, trend_granularity: str = "day") -> dict:
    normalized = _normalize_trend_granularity(trend_granularity)
    sync_state = await _safe_sync_ultramsg_self_messages(limit=100)
    return {
        "metrics": fetch_metrics(days),
        "by_category": fetch_distribution(days),
        "trends": fetch_trends(trend_days, granularity=normalized),
        "automation_recommendations": generate_automation_recommendations(days),
        "sync": sync_state,
    }


def _snapshot_fingerprint(snapshot: dict) -> str:
    payload = {
        "metrics": snapshot.get("metrics", {}),
        "by_category": snapshot.get("by_category", []),
        "trends": snapshot.get("trends", []),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


async def _get_or_build_explanations(snapshot: dict) -> tuple[dict[str, str], str, bool]:
    fingerprint = _snapshot_fingerprint(snapshot)

    async with _explanations_cache_lock:
        cached_fingerprint = _explanations_cache.get("fingerprint", "")
        cached_explanations = _explanations_cache.get("explanations", {})
        if cached_fingerprint == fingerprint and cached_explanations:
            return cached_explanations, fingerprint, True

        explanations = await generate_metric_explanations_parallel(
            metrics=snapshot["metrics"],
            by_category=snapshot["by_category"],
            trends=snapshot["trends"],
        )
        _explanations_cache["fingerprint"] = fingerprint
        _explanations_cache["explanations"] = explanations
        return explanations, fingerprint, False


@app.get("/api/dashboard", response_model=DashboardResponse)
async def dashboard(days: int = 30, trend_days: int = 30, trend_granularity: str = "day") -> dict:
    return await _build_dashboard_snapshot(days, trend_days, trend_granularity=trend_granularity)


@app.get("/api/dashboard/explanations")
async def dashboard_explanations(days: int = 30, trend_days: int = 30, trend_granularity: str = "day") -> dict:
    snapshot = await _build_dashboard_snapshot(days, trend_days, trend_granularity=trend_granularity)
    explanations, fingerprint, cached = await _get_or_build_explanations(snapshot)
    return {
        "fingerprint": fingerprint,
        "cached": cached,
        "explanations": explanations,
    }


@app.get("/api/dashboard/explanations/stream")
async def dashboard_explanations_stream(days: int = 30, trend_days: int = 30, trend_granularity: str = "day") -> StreamingResponse:
    snapshot = await _build_dashboard_snapshot(days, trend_days, trend_granularity=trend_granularity)
    explanations, _, _ = await _get_or_build_explanations(snapshot)

    def _line_stream():
        for label, text in explanations.items():
            yield f"Metric: {label}\n"
            if text:
                for line in str(text).splitlines():
                    yield f"{line}\n"
            yield "\n"

    return StreamingResponse(
        _line_stream(),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/whatsapp/messages")
async def whatsapp_messages(limit: int = 200) -> dict:
    return {"messages": fetch_recent_messages(limit=limit, source="whatsapp_self")}


@app.delete("/api/whatsapp/messages/{message_id}")
async def delete_whatsapp_message(message_id: int) -> dict:
    deleted = delete_message_by_id(message_id)
    if deleted:
        _notify_dashboard_clients()
    return {"deleted": deleted, "id": message_id}


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
