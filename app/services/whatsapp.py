from __future__ import annotations

import logging
import uuid
import re
from urllib.parse import parse_qsl, urlencode, urlsplit
from datetime import datetime, timezone

import httpx

from app.config import settings

logger = logging.getLogger("beastboard.whatsapp")

DASHBOARD_DATA_CUTOFF_UTC = datetime.fromisoformat(settings.dashboard_data_cutoff_iso)


def _gateway_base_url() -> str:
    return (settings.whatsapp_gateway_url or "").rstrip("/")


def _ultramsg_instance_id() -> str:
    base = _gateway_base_url()
    if not base:
        return ""
    return base.split("/")[-1]


def _with_token(url: str) -> str:
    token = settings.whatsapp_gateway_token or ""
    if not token:
        return url

    parsed = urlsplit(url)
    query_pairs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query_pairs["token"] = token

    encoded = urlencode(query_pairs)
    return parsed._replace(query=encoded).geturl()


def _status_from_payload(payload: dict) -> str:
    return (
        (payload.get("status") or {})
        .get("accountStatus", {})
        .get("status", "unknown")
    )


def _to_iso_from_unix(value: int | float | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _normalize_ultramsg_jid(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().lower().replace("+", "")


def _canonical_identity(value: str | None) -> str:
    normalized = _normalize_ultramsg_jid(value)
    if not normalized:
        return ""

    local_part = normalized.split("@", 1)[0]
    digits = re.sub(r"\D", "", local_part)
    return digits or local_part


def _is_dashboard_eligible_message(body: str, created_at: str | None) -> bool:
    if "[clawdbot]" in (body or "").lower():
        return False

    if not created_at:
        return False

    try:
        created_dt = datetime.fromisoformat(created_at)
    except ValueError:
        return False

    if created_dt.tzinfo is None:
        created_dt = created_dt.replace(tzinfo=timezone.utc)

    return created_dt >= DASHBOARD_DATA_CUTOFF_UTC


async def fetch_self_messages(limit: int = 100) -> list[dict]:
    """Fetch messages from the user's self-chat via /chats/messages API.

    The /messages endpoint only returns messages sent via the UltraMSG API.
    The /chats/messages endpoint returns ALL messages in a chat, including
    those sent directly from the WhatsApp app — which is what we need.
    """
    if not settings.whatsapp_gateway_url:
        logger.debug("No WHATSAPP_GATEWAY_URL configured, skipping fetch.")
        return []

    base = _gateway_base_url()
    logger.info("Fetching self-messages via /chats/messages (limit=%d)...", limit)

    async with httpx.AsyncClient(timeout=20) as client:
        me_response = await client.get(_with_token(f"{base}/instance/me"))
        me_response.raise_for_status()
        me_payload = me_response.json()
        own_id = _canonical_identity(
            me_payload.get("id")
            or me_payload.get("number")
            or me_payload.get("phone")
            or me_payload.get("jid")
        )
        if not own_id:
            logger.warning("Could not resolve own identity from /instance/me")
            return []
        logger.info("Own identity resolved: %s", own_id)

        self_chat_id = f"{own_id}@c.us"
        chat_url = _with_token(
            f"{base}/chats/messages?chatId={self_chat_id}&limit={limit}"
        )
        msg_response = await client.get(chat_url)
        msg_response.raise_for_status()
        msg_payload = msg_response.json()

    raw_messages = []
    if isinstance(msg_payload, list):
        raw_messages = msg_payload
    elif isinstance(msg_payload, dict):
        raw_messages = msg_payload.get("messages", []) or []

    logger.info("Raw self-chat messages fetched: %d", len(raw_messages))

    output: list[dict] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            continue

        if item.get("type") not in (None, "chat"):
            continue

        body = (item.get("body") or "").strip()
        if not body and isinstance(item.get("text"), dict):
            body = str((item.get("text") or {}).get("body") or "").strip()
        if not body and isinstance(item.get("text"), str):
            body = item.get("text").strip()
        if not body:
            continue

        msg_from = _canonical_identity(item.get("from"))
        msg_to = _canonical_identity(item.get("to"))

        if msg_from != own_id or msg_to != own_id:
            continue

        created_at = _to_iso_from_unix(item.get("timestamp"))
        if not created_at:
            created_at = _to_iso_from_unix(
                item.get("sent_at") or item.get("created_at")
            )

        if not _is_dashboard_eligible_message(body, created_at):
            continue

        output.append(
            {
                "external_message_id": str(
                    item.get("id") or item.get("message_id") or ""
                ) or None,
                "user_id": own_id,
                "message": body,
                "created_at": created_at,
            }
        )

    logger.info("Self-messages matched: %d", len(output))
    return output


async def start_session() -> dict:
    if not settings.whatsapp_gateway_url:
        session_id = f"demo-{uuid.uuid4().hex[:8]}"
        return {
            "session_id": session_id,
            "status": "waiting_for_qr_scan",
            "qr_code_data_url": "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyMDAiIGhlaWdodD0iMjAwIj48cmVjdCB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgZmlsbD0iI2VmZWJlNiIvPjx0ZXh0IHg9IjEwIiB5PSIxMDAiIGZvbnQtc2l6ZT0iMTIiIGZpbGw9IiMzMzMiPkNvbm5lY3QgV2hhdHNBcHAgUVIgKHNpbXVsYXRlZCk8L3RleHQ+PC9zdmc+",
            "note": "Set WHATSAPP_GATEWAY_URL to integrate real QR session flow (OpenClaw-style connector).",
        }

    base = _gateway_base_url()
    session_id = _ultramsg_instance_id() or f"instance-{uuid.uuid4().hex[:8]}"

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(_with_token(f"{base}/instance/status"))
        response.raise_for_status()
        status_payload = response.json()

    return {
        "session_id": session_id,
        "status": _status_from_payload(status_payload),
        "qr_code_data_url": _with_token(f"{base}/instance/qr"),
        "note": "UltraMsg instance status loaded. Scan the QR to authenticate this WhatsApp instance.",
    }


async def session_status(session_id: str) -> dict:
    if not settings.whatsapp_gateway_url:
        return {
            "session_id": session_id,
            "status": "waiting_for_qr_scan",
            "note": "Demo mode. Configure WHATSAPP_GATEWAY_URL for live status checks.",
        }

    base = _gateway_base_url()

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(_with_token(f"{base}/instance/status"))
        response.raise_for_status()
        payload = response.json()

    return {
        "session_id": session_id,
        "status": _status_from_payload(payload),
        "qr_code_data_url": _with_token(f"{base}/instance/qr"),
        "note": "Live status from UltraMsg.",
    }
