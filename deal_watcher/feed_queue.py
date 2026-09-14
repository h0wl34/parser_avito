from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from pathlib import Path

from .feed import ListingCandidate


@dataclass(frozen=True, slots=True)
class QueuedFeedEvent:
    event_key: str
    candidate: ListingCandidate
    attempts: int
    created_at: datetime


def candidate_event_key(candidate: ListingCandidate) -> str:
    """Stable dedupe key across providers/retries for one profile observation."""
    raw = f"{candidate.profile}\0{candidate.avito_id}\0{candidate.price}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _candidate_json(candidate: ListingCandidate) -> str:
    raw = asdict(candidate)
    if candidate.published_at is not None:
        raw["published_at"] = candidate.published_at.isoformat()
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))


class FeedEventQueue:
    """SQLite-backed at-least-once queue shared by ingress and worker.

    Provider retries are deduplicated by (profile, listing id, price). A worker
    claim has a lease, so a process crash cannot strand an event forever.
    """

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
                CREATE TABLE IF NOT EXISTS deal_feed_events (
                    event_key TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    avito_id INTEGER NOT NULL,
                    price INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    lease_until TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    processed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_deal_feed_due
                    ON deal_feed_events(status, next_attempt_at, lease_until);
                CREATE INDEX IF NOT EXISTS idx_deal_feed_created
                    ON deal_feed_events(created_at);
                """
            )

    def enqueue(
        self,
        candidate: ListingCandidate,
        *,
        observed_at: datetime | None = None,
    ) -> bool:
        now = observed_at or datetime.now(timezone.utc)
        stamp = now.isoformat()
        key = candidate_event_key(candidate)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO deal_feed_events (
                    event_key, profile, avito_id, price, source, mode,
                    payload_json, status, attempts, next_attempt_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    key,
                    candidate.profile,
                    candidate.avito_id,
                    candidate.price,
                    candidate.source,
                    candidate.mode,
                    _candidate_json(candidate),
                    stamp,
                    stamp,
                    stamp,
                ),
            )
            return cursor.rowcount == 1

    def claim_due(
        self,
        *,
        lease_seconds: int = 120,
        now: datetime | None = None,
    ) -> QueuedFeedEvent | None:
        now = now or datetime.now(timezone.utc)
        now_stamp = now.isoformat()
        lease_until = (now + timedelta(seconds=max(10, lease_seconds))).isoformat()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT *
                FROM deal_feed_events
                WHERE (
                    status = 'pending' AND next_attempt_at <= ?
                ) OR (
                    status = 'processing'
                    AND lease_until IS NOT NULL
                    AND lease_until <= ?
                )
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now_stamp, now_stamp),
            ).fetchone()
            if row is None:
                conn.commit()
                return None

            attempts = int(row["attempts"]) + 1
            conn.execute(
                """
                UPDATE deal_feed_events
                SET status='processing', attempts=?, lease_until=?, updated_at=?
                WHERE event_key=?
                """,
                (attempts, lease_until, now_stamp, row["event_key"]),
            )
            conn.commit()

            created_at = datetime.fromisoformat(row["created_at"])
            return QueuedFeedEvent(
                event_key=str(row["event_key"]),
                candidate=ListingCandidate.from_json(str(row["payload_json"])),
                attempts=attempts,
                created_at=created_at,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ack(self, event_key: str, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        stamp = now.isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE deal_feed_events
                SET status='done', processed_at=?, updated_at=?, lease_until=NULL,
                    last_error=NULL
                WHERE event_key=?
                """,
                (stamp, stamp, event_key),
            )

    def retry(
        self,
        event_key: str,
        *,
        error: str,
        delay_seconds: int,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        next_at = now + timedelta(seconds=max(1, delay_seconds))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE deal_feed_events
                SET status='pending', next_attempt_at=?, updated_at=?,
                    lease_until=NULL, last_error=?
                WHERE event_key=?
                """,
                (
                    next_at.isoformat(),
                    now.isoformat(),
                    str(error)[:1000],
                    event_key,
                ),
            )

    def dead_letter(
        self,
        event_key: str,
        *,
        error: str,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE deal_feed_events
                SET status='dead', updated_at=?, lease_until=NULL, last_error=?
                WHERE event_key=?
                """,
                (now.isoformat(), str(error)[:1000], event_key),
            )

    def stats(self) -> dict[str, int]:
        result = {"pending": 0, "processing": 0, "done": 0, "dead": 0}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM deal_feed_events GROUP BY status"
            ).fetchall()
        for row in rows:
            result[str(row["status"])] = int(row["n"])
        return result

    def prune_done(self, *, keep_days: int = 14, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=max(1, keep_days))).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM deal_feed_events
                WHERE status='done' AND processed_at IS NOT NULL AND processed_at < ?
                """,
                (cutoff,),
            )
            return int(cursor.rowcount)
