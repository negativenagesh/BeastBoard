from __future__ import annotations

import sqlite3
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings


DASHBOARD_DATA_CUTOFF_ISO = settings.dashboard_data_cutoff_iso
CLAWDBOT_MARKER = "[clawdbot]"


def _canonical_whatsapp_user_id(value: str | None) -> str:
    if not value:
        return ""
    normalized = str(value).strip().lower().replace("+", "")
    local_part = normalized.split("@", 1)[0]
    digits = re.sub(r"\D", "", local_part)
    return digits or local_part


def _db_path() -> str:
    prefix = "sqlite:///"
    if settings.database_url.startswith(prefix):
        raw = settings.database_url[len(prefix) :]
        path = Path(raw)
        if path.is_absolute():
            return str(path)
        project_root = Path(__file__).resolve().parent.parent
        return str((project_root / path).resolve())
    return settings.database_url


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _dashboard_filter_clause(*, created_at_col: str = "created_at", source_col: str = "source", message_col: str = "raw_message") -> str:
    return (
        f"datetime({created_at_col}) >= datetime(?) "
        f"AND {source_col} = 'whatsapp_self' "
        f"AND lower({message_col}) NOT LIKE ?"
    )


def _dashboard_filter_params() -> tuple[str, str]:
    return (DASHBOARD_DATA_CUTOFF_ISO, f"%{CLAWDBOT_MARKER}%")


def _trend_bucket_expression(granularity: str) -> str:
    if granularity == "week":
        return "strftime('%Y-W%W', created_at)"
    if granularity == "month":
        return "strftime('%Y-%m', created_at)"
    return "date(created_at)"


def init_db() -> None:
    with closing(get_conn()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                user_id TEXT,
                external_message_id TEXT,
                raw_message TEXT NOT NULL,
                category TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                suggested_action TEXT NOT NULL
            )
            """
        )
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(support_messages)").fetchall()}
        if "external_message_id" not in cols:
            conn.execute("ALTER TABLE support_messages ADD COLUMN external_message_id TEXT")

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_support_messages_source_external
            ON support_messages(source, external_message_id)
            WHERE external_message_id IS NOT NULL
            """
        )
        conn.execute(
            """
            DELETE FROM support_messages
            WHERE source = 'whatsapp_self'
              AND (
                datetime(created_at) < datetime(?)
                OR lower(raw_message) LIKE ?
              )
            """,
            (DASHBOARD_DATA_CUTOFF_ISO, f"%{CLAWDBOT_MARKER}%"),
        )
        conn.commit()


def purge_filtered_whatsapp_messages() -> int:
    """Delete old or bot-tagged WhatsApp self messages from DB."""
    with closing(get_conn()) as conn:
        cur = conn.execute(
            """
            DELETE FROM support_messages
            WHERE source = 'whatsapp_self'
              AND (
                datetime(created_at) < datetime(?)
                OR lower(raw_message) LIKE ?
              )
            """,
            (DASHBOARD_DATA_CUTOFF_ISO, f"%{CLAWDBOT_MARKER}%"),
        )
        conn.commit()
        return cur.rowcount


def normalize_whatsapp_self_user_ids() -> int:
    """Normalize whatsapp_self user IDs to a canonical phone-number identity."""
    updated = 0
    with closing(get_conn()) as conn:
        rows = conn.execute(
            """
            SELECT id, user_id
            FROM support_messages
            WHERE source = 'whatsapp_self'
            """
        ).fetchall()

        for row in rows:
            canonical = _canonical_whatsapp_user_id(row["user_id"])
            if canonical and canonical != (row["user_id"] or ""):
                conn.execute(
                    "UPDATE support_messages SET user_id = ? WHERE id = ?",
                    (canonical, int(row["id"])),
                )
                updated += 1

        conn.commit()

    return updated


def purge_whatsapp_self_outlier_users() -> int:
    """Keep only the dominant whatsapp_self user_id and remove outlier identities."""
    with closing(get_conn()) as conn:
        dominant_row = conn.execute(
            """
            SELECT user_id, COUNT(*) AS total
            FROM support_messages
            WHERE source = 'whatsapp_self'
              AND user_id IS NOT NULL
              AND TRIM(user_id) <> ''
            GROUP BY user_id
            ORDER BY total DESC
            LIMIT 1
            """
        ).fetchone()

        if not dominant_row:
            return 0

        dominant_user_id = dominant_row["user_id"]
        cur = conn.execute(
            """
            DELETE FROM support_messages
            WHERE source = 'whatsapp_self'
              AND user_id IS NOT NULL
              AND TRIM(user_id) <> ''
              AND user_id <> ?
            """,
            (dominant_user_id,),
        )
        conn.commit()
        return cur.rowcount


def message_exists_by_external_id(source: str, external_message_id: str | None) -> bool:
    """Return True if a message with this source + external_message_id is already stored."""
    if not external_message_id:
        return False
    with closing(get_conn()) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM support_messages
            WHERE source = ? AND external_message_id = ?
            LIMIT 1
            """,
            (source, external_message_id),
        ).fetchone()
    return row is not None


def insert_message(
    *,
    source: str,
    user_id: str | None,
    external_message_id: str | None = None,
    raw_message: str,
    category: str,
    confidence: float,
    reason: str,
    suggested_action: str,
    created_at: str | None = None,
) -> dict:
    if source == "whatsapp_self":
        user_id = _canonical_whatsapp_user_id(user_id)

    created_at_value = created_at or datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

    with closing(get_conn()) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO support_messages (
                created_at,
                source,
                user_id,
                external_message_id,
                raw_message,
                category,
                confidence,
                reason,
                suggested_action
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at_value,
                source,
                user_id,
                external_message_id,
                raw_message,
                category,
                confidence,
                reason,
                suggested_action,
            ),
        )

        inserted = cur.rowcount > 0
        if inserted:
            row_id = cur.lastrowid
            row = conn.execute("SELECT * FROM support_messages WHERE id = ?", (row_id,)).fetchone()
        elif external_message_id:
            row = conn.execute(
                """
                SELECT * FROM support_messages
                WHERE source = ? AND external_message_id = ?
                """,
                (source, external_message_id),
            ).fetchone()
        else:
            row = None

        conn.commit()

    if not row:
        return {
            "id": -1,
            "created_at": created_at_value,
            "source": source,
            "user_id": user_id,
            "external_message_id": external_message_id,
            "message": raw_message,
            "category": category,
            "confidence": confidence,
            "reason": reason,
            "suggested_action": suggested_action,
            "inserted": inserted,
        }

    return {
        "id": int(row["id"]),
        "created_at": row["created_at"],
        "source": row["source"],
        "user_id": row["user_id"],
        "external_message_id": row["external_message_id"],
        "message": row["raw_message"],
        "category": row["category"],
        "confidence": float(row["confidence"]),
        "reason": row["reason"],
        "suggested_action": row["suggested_action"],
        "inserted": inserted,
    }


def fetch_distribution(days: int = 30) -> list[dict]:
    with closing(get_conn()) as conn:
        rows = conn.execute(
            """
            SELECT category, COUNT(*) as count
            FROM support_messages
            WHERE datetime(created_at) >= datetime('now', ?)
              AND """ + _dashboard_filter_clause() + """
            GROUP BY category
            ORDER BY count DESC
            """,
            (f"-{days} days", *_dashboard_filter_params()),
        ).fetchall()

    total = sum(r["count"] for r in rows)
    if total == 0:
        return []

    return [
        {
            "category": r["category"],
            "count": int(r["count"]),
            "percentage": round((r["count"] / total) * 100, 2),
        }
        for r in rows
    ]


def fetch_trends(days: int = 14, granularity: str = "day") -> list[dict]:
    normalized_granularity = granularity if granularity in {"day", "week", "month"} else "day"
    bucket_expr = _trend_bucket_expression(normalized_granularity)

    with closing(get_conn()) as conn:
        rows = conn.execute(
            """
            SELECT """ + bucket_expr + """ as date_key, category, COUNT(*) as count
            FROM support_messages
            WHERE datetime(created_at) >= datetime('now', ?)
              AND """ + _dashboard_filter_clause() + """
            GROUP BY date_key, category
            ORDER BY date_key ASC
            """,
            (f"-{days} days", *_dashboard_filter_params()),
        ).fetchall()

    return [
        {"date": r["date_key"], "category": r["category"], "count": int(r["count"])}
        for r in rows
    ]


def count_messages(days: int = 30) -> int:
    with closing(get_conn()) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) as total
            FROM support_messages
            WHERE datetime(created_at) >= datetime('now', ?)
              AND """ + _dashboard_filter_clause() + """
            """,
            (f"-{days} days", *_dashboard_filter_params()),
        ).fetchone()

    return int(row["total"]) if row else 0


def fetch_recent_messages(limit: int = 200, source: str = "whatsapp_self") -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 1000))
    where_clause = "WHERE source = ?"
    params: list[Any] = [source]
    if source == "whatsapp_self":
        where_clause += " AND " + _dashboard_filter_clause()
        params.extend(_dashboard_filter_params())
    params.append(safe_limit)

    with closing(get_conn()) as conn:
        rows = conn.execute(
            f"""
            SELECT
                id,
                created_at,
                source,
                user_id,
                external_message_id,
                raw_message,
                category,
                confidence
            FROM support_messages
            {where_clause}
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()

    return [
        {
            "id": int(r["id"]),
            "created_at": r["created_at"],
            "source": r["source"],
            "user_id": r["user_id"],
            "external_message_id": r["external_message_id"],
            "message": r["raw_message"],
            "category": r["category"],
            "confidence": float(r["confidence"]),
        }
        for r in rows
    ]


def delete_message_by_id(message_id: int) -> bool:
    with closing(get_conn()) as conn:
        cur = conn.execute("DELETE FROM support_messages WHERE id = ?", (int(message_id),))
        conn.commit()
        return cur.rowcount > 0


def _count_between(conn: sqlite3.Connection, start: datetime, end: datetime) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM support_messages
        WHERE datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
                    AND """ + _dashboard_filter_clause() + """
        """,
                (start.isoformat(), end.isoformat(), *_dashboard_filter_params()),
    ).fetchone()
    return int(row["total"]) if row else 0


def _safe_growth(current: int, previous: int) -> float:
    if previous <= 0:
        return 100.0 if current > 0 else 0.0
    return round(((current - previous) / previous) * 100, 2)


def fetch_metrics(days: int = 30) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    start_30 = now - timedelta(days=days)
    start_7 = now - timedelta(days=7)
    start_14 = now - timedelta(days=14)
    start_60 = now - timedelta(days=60)
    start_today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    with closing(get_conn()) as conn:
        total_30 = _count_between(conn, start_30, now)
        total_7 = _count_between(conn, start_7, now)
        prev_7 = _count_between(conn, start_14, start_7)
        prev_30 = _count_between(conn, start_60, start_30)
        queries_today = _count_between(conn, start_today, now)

        unique_customers_row = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id) AS total
            FROM support_messages
            WHERE datetime(created_at) >= datetime(?)
                            AND """ + _dashboard_filter_clause() + """
              AND user_id IS NOT NULL
                            AND TRIM(user_id) <> ''
            """,
                        (start_30.isoformat(), *_dashboard_filter_params()),
        ).fetchone()
        unique_customers = int(unique_customers_row["total"]) if unique_customers_row else 0

        active_days_row = conn.execute(
            """
            SELECT COUNT(DISTINCT date(created_at)) AS total
            FROM support_messages
            WHERE datetime(created_at) >= datetime(?)
                            AND """ + _dashboard_filter_clause() + """
            """,
                        (start_30.isoformat(), *_dashboard_filter_params()),
        ).fetchone()
        active_days = int(active_days_row["total"]) if active_days_row else 0

        avg_conf_row = conn.execute(
            """
            SELECT AVG(confidence) AS avg_confidence
            FROM support_messages
            WHERE datetime(created_at) >= datetime(?)
                            AND """ + _dashboard_filter_clause() + """
            """,
                        (start_30.isoformat(), *_dashboard_filter_params()),
        ).fetchone()
        avg_confidence = float(avg_conf_row["avg_confidence"] or 0.0)

        high_conf_row = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM support_messages
            WHERE datetime(created_at) >= datetime(?)
                            AND """ + _dashboard_filter_clause() + """
              AND confidence >= 0.8
            """,
                        (start_30.isoformat(), *_dashboard_filter_params()),
        ).fetchone()
        high_conf_count = int(high_conf_row["total"]) if high_conf_row else 0

        top_category_row = conn.execute(
            """
            SELECT category, COUNT(*) AS count
            FROM support_messages
            WHERE datetime(created_at) >= datetime(?)
                            AND """ + _dashboard_filter_clause() + """
            GROUP BY category
            ORDER BY count DESC
            LIMIT 1
            """,
                        (start_30.isoformat(), *_dashboard_filter_params()),
        ).fetchone()

        repeat_customers_row = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM (
                SELECT user_id
                FROM support_messages
                WHERE datetime(created_at) >= datetime(?)
                  AND """ + _dashboard_filter_clause() + """
                  AND user_id IS NOT NULL
                  AND TRIM(user_id) <> ''
                GROUP BY user_id
                HAVING COUNT(*) > 1
            )
            """,
            (start_30.isoformat(), *_dashboard_filter_params()),
        ).fetchone()
        repeat_customers = int(repeat_customers_row["total"]) if repeat_customers_row else 0

    top_category = top_category_row["category"] if top_category_row else "-"
    top_category_count = int(top_category_row["count"]) if top_category_row else 0
    top_category_pct = round((top_category_count / total_30) * 100, 2) if total_30 else 0.0

    return {
        "total_queries_30d": total_30,
        "queries_today": queries_today,
        "queries_last_7d": total_7,
        "unique_customers_30d": unique_customers,
        "active_days_30d": active_days,
        "avg_queries_per_active_day": round(total_30 / active_days, 2) if active_days else 0.0,
        "avg_daily_queries_7d": round(total_7 / 7, 2),
        "avg_classification_confidence": round(avg_confidence, 3),
        "high_confidence_rate_pct": round((high_conf_count / total_30) * 100, 2) if total_30 else 0.0,
        "top_issue_type": top_category,
        "top_issue_share_pct": top_category_pct,
        "repeat_customer_rate_pct": round((repeat_customers / unique_customers) * 100, 2)
        if unique_customers
        else 0.0,
        "week_over_week_growth_pct": _safe_growth(total_7, prev_7),
        "month_over_month_growth_pct": _safe_growth(total_30, prev_30),
    }
