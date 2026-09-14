from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any

import requests

from integrations.notifications.proxy import build_requests_proxy


@dataclass(frozen=True, slots=True)
class Heartbeat:
    service: str
    updated_at: datetime
    details: str | None


@dataclass(frozen=True, slots=True)
class PendingSystemAlert:
    alert_id: int
    incident_key: str
    kind: str
    severity: str
    message: str
    attempts: int


@dataclass(frozen=True, slots=True)
class QueueHealthSnapshot:
    pending: int
    processing: int
    done: int
    dead: int
    oldest_unfinished_age_seconds: float | None


class HealthStore:
    """Persistent health state, incident debounce, and system-alert outbox."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS deal_health_heartbeats (
                    service TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    details TEXT
                );

                CREATE TABLE IF NOT EXISTS deal_health_incidents (
                    incident_key TEXT PRIMARY KEY,
                    severity TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    first_failure_at TEXT,
                    last_failure_at TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    summary TEXT,
                    opened_at TEXT,
                    last_notified_at TEXT,
                    recovered_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deal_system_alert_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    last_error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_system_alert_due
                    ON deal_system_alert_outbox(status, next_attempt_at);
                """
            )

    def touch_heartbeat(
        self,
        service: str,
        *,
        details: str | dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        if isinstance(details, dict):
            details_value = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        elif details is None:
            details_value = None
        else:
            details_value = str(details)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO deal_health_heartbeats(service, updated_at, details)
                VALUES (?, ?, ?)
                ON CONFLICT(service) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    details=excluded.details
                """,
                (service, now.isoformat(), details_value),
            )

    def heartbeat(self, service: str) -> Heartbeat | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT service, updated_at, details FROM deal_health_heartbeats WHERE service=?",
                (service,),
            ).fetchone()
        if row is None:
            return None
        return Heartbeat(
            service=str(row["service"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            details=str(row["details"]) if row["details"] is not None else None,
        )

    def _enqueue_alert(
        self,
        conn: sqlite3.Connection,
        *,
        incident_key: str,
        kind: str,
        severity: str,
        message: str,
        now: datetime,
    ) -> None:
        stamp = now.isoformat()
        conn.execute(
            """
            INSERT INTO deal_system_alert_outbox(
                incident_key, kind, severity, message, status, attempts,
                next_attempt_at, created_at
            ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (incident_key, kind, severity, message, stamp, stamp),
        )

    def observe(
        self,
        incident_key: str,
        *,
        healthy: bool,
        severity: str,
        summary: str,
        grace_seconds: int,
        consecutive_required: int,
        repeat_alert_seconds: int,
        now: datetime | None = None,
    ) -> str:
        """Observe a condition and debounce it into persistent incident state.

        Returns one of: ``healthy``, ``debouncing``, ``opened``, ``active``,
        ``repeated``, ``recovered``.
        """
        now = now or datetime.now(timezone.utc)
        stamp = now.isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deal_health_incidents WHERE incident_key=?",
                (incident_key,),
            ).fetchone()

            if healthy:
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO deal_health_incidents(
                            incident_key, severity, active, consecutive_failures,
                            summary, updated_at
                        ) VALUES (?, ?, 0, 0, ?, ?)
                        """,
                        (incident_key, severity, summary, stamp),
                    )
                    return "healthy"

                was_active = bool(row["active"])
                opened_at = (
                    datetime.fromisoformat(str(row["opened_at"]))
                    if row["opened_at"]
                    else None
                )
                conn.execute(
                    """
                    UPDATE deal_health_incidents
                    SET active=0, consecutive_failures=0, first_failure_at=NULL,
                        last_failure_at=NULL, summary=?, recovered_at=?, updated_at=?
                    WHERE incident_key=?
                    """,
                    (summary, stamp if was_active else row["recovered_at"], stamp, incident_key),
                )
                if was_active:
                    duration = ""
                    if opened_at is not None:
                        seconds = max(0, int((now - opened_at).total_seconds()))
                        duration = f" Downtime: {format_duration(seconds)}."
                    self._enqueue_alert(
                        conn,
                        incident_key=incident_key,
                        kind="recovery",
                        severity="INFO",
                        message=f"✅ RECOVERED [{incident_key}] {summary}.{duration}",
                        now=now,
                    )
                    return "recovered"
                return "healthy"

            first_failure_at = now
            consecutive = 1
            active = False
            opened_at = None
            last_notified_at = None
            if row is not None:
                consecutive = int(row["consecutive_failures"] or 0) + 1
                active = bool(row["active"])
                if row["first_failure_at"]:
                    first_failure_at = datetime.fromisoformat(str(row["first_failure_at"]))
                if row["opened_at"]:
                    opened_at = datetime.fromisoformat(str(row["opened_at"]))
                if row["last_notified_at"]:
                    last_notified_at = datetime.fromisoformat(str(row["last_notified_at"]))

            if row is None:
                conn.execute(
                    """
                    INSERT INTO deal_health_incidents(
                        incident_key, severity, active, first_failure_at,
                        last_failure_at, consecutive_failures, summary, updated_at
                    ) VALUES (?, ?, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        incident_key,
                        severity,
                        first_failure_at.isoformat(),
                        stamp,
                        consecutive,
                        summary,
                        stamp,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE deal_health_incidents
                    SET severity=?, last_failure_at=?, consecutive_failures=?,
                        summary=?, updated_at=?
                    WHERE incident_key=?
                    """,
                    (severity, stamp, consecutive, summary, stamp, incident_key),
                )

            failure_age = (now - first_failure_at).total_seconds()
            should_open = (
                not active
                and consecutive >= max(1, consecutive_required)
                and failure_age >= max(0, grace_seconds)
            )
            if should_open:
                conn.execute(
                    """
                    UPDATE deal_health_incidents
                    SET active=1, opened_at=?, last_notified_at=?, updated_at=?
                    WHERE incident_key=?
                    """,
                    (stamp, stamp, stamp, incident_key),
                )
                self._enqueue_alert(
                    conn,
                    incident_key=incident_key,
                    kind="open",
                    severity=severity,
                    message=f"🚨 {severity} [{incident_key}] {summary}",
                    now=now,
                )
                return "opened"

            if active:
                repeat_due = (
                    last_notified_at is None
                    or (now - last_notified_at).total_seconds() >= repeat_alert_seconds
                )
                if repeat_due:
                    opened = opened_at or first_failure_at
                    duration = max(0, int((now - opened).total_seconds()))
                    conn.execute(
                        """
                        UPDATE deal_health_incidents
                        SET last_notified_at=?, updated_at=?
                        WHERE incident_key=?
                        """,
                        (stamp, stamp, incident_key),
                    )
                    self._enqueue_alert(
                        conn,
                        incident_key=incident_key,
                        kind="repeat",
                        severity=severity,
                        message=(
                            f"🚨 STILL {severity} [{incident_key}] {summary}. "
                            f"Active for {format_duration(duration)}."
                        ),
                        now=now,
                    )
                    return "repeated"
                return "active"

            return "debouncing"

    def claim_alert(self, *, now: datetime | None = None) -> PendingSystemAlert | None:
        now = now or datetime.now(timezone.utc)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM deal_system_alert_outbox
                WHERE status='pending' AND next_attempt_at <= ?
                ORDER BY id ASC LIMIT 1
                """,
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            attempts = int(row["attempts"]) + 1
            conn.execute(
                "UPDATE deal_system_alert_outbox SET status='sending', attempts=? WHERE id=?",
                (attempts, row["id"]),
            )
            conn.commit()
            return PendingSystemAlert(
                alert_id=int(row["id"]),
                incident_key=str(row["incident_key"]),
                kind=str(row["kind"]),
                severity=str(row["severity"]),
                message=str(row["message"]),
                attempts=attempts,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def alert_delivered(
        self,
        alert_id: int,
        *,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE deal_system_alert_outbox
                SET status='done', delivered_at=?, last_error=NULL
                WHERE id=?
                """,
                (now.isoformat(), alert_id),
            )

    def alert_retry(
        self,
        alert_id: int,
        *,
        error: str,
        attempts: int,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        delay = min(900, (5, 15, 30, 60, 120, 300)[min(max(1, attempts), 6) - 1])
        next_at = now + timedelta(seconds=delay)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE deal_system_alert_outbox
                SET status='pending', next_attempt_at=?, last_error=?
                WHERE id=?
                """,
                (next_at.isoformat(), str(error)[:1000], alert_id),
            )

    def reset_stuck_alerts(
        self,
        *,
        now: datetime | None = None,
    ) -> int:
        """Recover alerts left in sending state by a crashed health monitor."""
        now = now or datetime.now(timezone.utc)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE deal_system_alert_outbox
                SET status='pending', next_attempt_at=?
                WHERE status='sending'
                """,
                (now.isoformat(),),
            )
            return int(cursor.rowcount)

    def active_incidents(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT incident_key, severity, summary, opened_at, last_failure_at
                FROM deal_health_incidents
                WHERE active=1
                ORDER BY severity DESC, opened_at ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]


def format_duration(seconds: int | float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def queue_health_snapshot(
    db_path: str | Path,
    *,
    now: datetime | None = None,
) -> QueueHealthSnapshot:
    now = now or datetime.now(timezone.utc)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        counts = {"pending": 0, "processing": 0, "done": 0, "dead": 0}
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM deal_feed_events GROUP BY status"
        ).fetchall()
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        oldest = conn.execute(
            """
            SELECT MIN(created_at) AS oldest
            FROM deal_feed_events
            WHERE status IN ('pending', 'processing')
            """
        ).fetchone()
        oldest_age = None
        if oldest is not None and oldest["oldest"]:
            created = datetime.fromisoformat(str(oldest["oldest"]))
            oldest_age = max(0.0, (now - created).total_seconds())
        return QueueHealthSnapshot(
            pending=counts["pending"],
            processing=counts["processing"],
            done=counts["done"],
            dead=counts["dead"],
            oldest_unfinished_age_seconds=oldest_age,
        )
    finally:
        conn.close()


def disk_free_mb(path: str | Path) -> int:
    target = Path(path).resolve()
    if target.is_file():
        target = target.parent
    usage = shutil.disk_usage(target)
    return int(usage.free / (1024 * 1024))


def verify_relay_signature(
    raw_body: bytes,
    *,
    timestamp: str | None,
    signature: str | None,
    secret: str,
    max_skew_seconds: int = 300,
    now_ms: int | None = None,
) -> bool:
    if not timestamp or not signature or not secret:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if abs(current - ts) > max_skew_seconds * 1000:
        return False
    digest = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, f"sha256={digest}")


class SystemAlertSender:
    """Deliver health alerts over primary Telegram with an independent relay fallback."""

    def __init__(
        self,
        *,
        bot_token: str | None,
        chat_ids: list[str] | list[int] | None,
        proxy: str | None,
        relay_url: str | None = None,
        relay_secret: str | None = None,
    ):
        self.bot_token = (bot_token or "").strip()
        self.chat_ids = [str(value) for value in (chat_ids or []) if str(value).strip()]
        self.proxy = build_requests_proxy(proxy)
        self.relay_url = (relay_url or "").strip()
        self.relay_secret = (relay_secret or "").strip()

    def probe_primary_telegram(self) -> tuple[bool, str]:
        if not self.bot_token:
            return False, "Telegram bot token is not configured"
        if not self.chat_ids:
            return False, "Telegram chat id is not configured"
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{self.bot_token}/getMe",
                proxies=self.proxy,
                timeout=10,
            )
            if response.status_code != 200:
                return False, f"Telegram Bot API HTTP {response.status_code}"
            payload = response.json()
            if not payload.get("ok"):
                return False, "Telegram Bot API returned ok=false"
            return True, "Telegram notifier path is reachable"
        except requests.RequestException as err:
            return False, f"Telegram notifier path failed: {type(err).__name__}"
        except (ValueError, TypeError):
            return False, "Telegram Bot API returned invalid JSON"

    def _send_primary(self, message: str) -> tuple[bool, str]:
        if not self.bot_token or not self.chat_ids:
            return False, "primary Telegram is not configured"
        errors: list[str] = []
        delivered = False
        for chat_id in self.chat_ids:
            try:
                response = requests.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": message},
                    proxies=self.proxy,
                    timeout=10,
                )
                if response.status_code == 200:
                    delivered = True
                else:
                    errors.append(f"HTTP {response.status_code}")
            except requests.RequestException as err:
                errors.append(type(err).__name__)
        return delivered, ", ".join(errors) if errors else "primary delivery failed"

    def _send_relay(self, message: str) -> tuple[bool, str]:
        if not self.relay_url or not self.relay_secret:
            return False, "emergency relay is not configured"
        body = json.dumps(
            {"text": message},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(time.time() * 1000))
        digest = hmac.new(
            self.relay_secret.encode("utf-8"),
            timestamp.encode("utf-8") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        try:
            response = requests.post(
                self.relay_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Deal-Timestamp": timestamp,
                    "X-Deal-Signature": f"sha256={digest}",
                },
                timeout=10,
            )
            if 200 <= response.status_code < 300:
                return True, "delivered through emergency relay"
            return False, f"relay HTTP {response.status_code}"
        except requests.RequestException as err:
            return False, f"relay {type(err).__name__}"

    def send(self, message: str) -> tuple[bool, str]:
        primary_ok, primary_detail = self._send_primary(message)
        if primary_ok:
            return True, "primary Telegram"
        relay_ok, relay_detail = self._send_relay(message)
        if relay_ok:
            return True, "emergency relay"
        return False, f"primary={primary_detail}; relay={relay_detail}"
