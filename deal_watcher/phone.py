from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


_EVENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def compute_phone_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Return the lowercase HMAC-SHA256 hex digest used by the phone bridge."""
    message = timestamp.encode("utf-8") + b"." + raw_body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_phone_signature(
    raw_body: bytes,
    *,
    timestamp: str | None,
    signature: str | None,
    secret: str,
    max_skew_seconds: int = 300,
    now: datetime | None = None,
) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False

    current = now or datetime.now(timezone.utc)
    if abs(int(current.timestamp()) - sent_at) > max_skew_seconds:
        return False

    supplied = signature.strip().lower()
    if supplied.startswith("sha256="):
        supplied = supplied[len("sha256=") :]
    expected = compute_phone_signature(secret, timestamp, raw_body)
    return hmac.compare_digest(expected, supplied)


def _require_string(payload: dict[str, Any], key: str, *, max_len: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{key} must be a non-empty string <= {max_len} chars")
    return value


def _optional_string(payload: dict[str, Any], key: str, *, max_len: int) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_len:
        raise ValueError(f"{key} must be a string <= {max_len} chars")
    return value


def validate_phone_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if payload.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")

    event_id = _require_string(payload, "event_id", max_len=64).lower()
    if not _EVENT_ID_RE.fullmatch(event_id):
        raise ValueError("event_id must be 64 lowercase hexadecimal characters")
    payload["event_id"] = event_id

    _require_string(payload, "device_id", max_len=128)
    _require_string(payload, "package", max_len=255)
    _require_string(payload, "source", max_len=64)
    _optional_string(payload, "notification_key", max_len=2048)
    _optional_string(payload, "title", max_len=8192)
    _optional_string(payload, "text", max_len=16384)
    _optional_string(payload, "big_text", max_len=32768)

    for key in ("captured_at_ms", "post_time_ms"):
        value = payload.get(key)
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")

    extras = payload.get("extras", {})
    if not isinstance(extras, dict):
        raise ValueError("extras must be an object")
    actions = payload.get("actions", [])
    if not isinstance(actions, list):
        raise ValueError("actions must be an array")
    return payload


@dataclass(frozen=True)
class PhoneNotificationRow:
    event_id: str
    device_id: str
    package_name: str
    title: str | None
    text: str | None
    captured_at_ms: int
    received_at: str
    payload_json: str


class PhoneNotificationStore:
    """Durable raw Android notification inbox.

    Raw push payloads are intentionally kept separate from ListingCandidate until
    we have observed Avito's real notification schema. event_id is the durable
    deduplication key, so WorkManager retries are safe.
    """

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deal_phone_notifications (
                    event_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    package_name TEXT NOT NULL,
                    notification_key TEXT,
                    notification_id INTEGER,
                    title TEXT,
                    text TEXT,
                    post_time_ms INTEGER NOT NULL,
                    captured_at_ms INTEGER NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_deal_phone_received
                ON deal_phone_notifications(received_at DESC)
                """
            )

    def store(
        self,
        payload: dict[str, Any],
        *,
        raw_body: bytes | None = None,
        now: datetime | None = None,
    ) -> bool:
        validated = validate_phone_payload(payload)
        received = (now or datetime.now(timezone.utc)).isoformat()
        if raw_body is None:
            payload_json = json.dumps(
                validated,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        else:
            payload_json = raw_body.decode("utf-8")

        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO deal_phone_notifications (
                    event_id, device_id, package_name, notification_key,
                    notification_id, title, text, post_time_ms,
                    captured_at_ms, received_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validated["event_id"],
                    validated["device_id"],
                    validated["package"],
                    validated.get("notification_key"),
                    validated.get("notification_id"),
                    validated.get("title"),
                    validated.get("text"),
                    validated["post_time_ms"],
                    validated["captured_at_ms"],
                    received,
                    payload_json,
                ),
            )
            return cursor.rowcount == 1

    def recent(self, limit: int = 20) -> list[PhoneNotificationRow]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, device_id, package_name, title, text,
                       captured_at_ms, received_at, payload_json
                FROM deal_phone_notifications
                ORDER BY received_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            PhoneNotificationRow(
                event_id=row["event_id"],
                device_id=row["device_id"],
                package_name=row["package_name"],
                title=row["title"],
                text=row["text"],
                captured_at_ms=row["captured_at_ms"],
                received_at=row["received_at"],
                payload_json=row["payload_json"],
            )
            for row in rows
        ]

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM deal_phone_notifications").fetchone()
        return int(row["n"])
