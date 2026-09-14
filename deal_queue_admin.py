from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sqlite3

from deal_watcher import load_deal_watcher_config
from deal_watcher.feed_queue import FeedEventQueue


def _db_path() -> str:
    return load_deal_watcher_config("deal_watcher.toml").database_path


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def cmd_stats(args) -> int:
    queue = FeedEventQueue(args.database)
    stats = queue.stats()
    for key in ("pending", "processing", "done", "dead"):
        print(f"{key}: {stats.get(key, 0)}")
    return 0


def cmd_dead(args) -> int:
    with _connect(args.database) as conn:
        rows = conn.execute(
            """
            SELECT event_key, profile, avito_id, price, attempts, updated_at,
                   last_error
            FROM deal_feed_events
            WHERE status='dead'
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
    if not rows:
        print("No dead-letter events.")
        return 0
    for row in rows:
        print(
            f"{row['event_key'][:16]}  profile={row['profile']} "
            f"id={row['avito_id']} price={row['price']} "
            f"attempts={row['attempts']} updated={row['updated_at']}"
        )
        if row["last_error"]:
            print(f"  error: {row['last_error']}")
    return 0


def cmd_retry_dead(args) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(args.database) as conn:
        if args.all:
            cursor = conn.execute(
                """
                UPDATE deal_feed_events
                SET status='pending', attempts=0, next_attempt_at=?,
                    lease_until=NULL, last_error=NULL, updated_at=?
                WHERE status='dead'
                """,
                (now, now),
            )
        else:
            prefix = (args.event or "").strip().lower()
            if not prefix:
                raise SystemExit("retry-dead requires EVENT_PREFIX or --all")
            matches = conn.execute(
                """
                SELECT event_key FROM deal_feed_events
                WHERE status='dead' AND lower(event_key) LIKE ?
                """,
                (prefix + "%",),
            ).fetchall()
            if len(matches) != 1:
                raise SystemExit(
                    f"EVENT_PREFIX must match exactly one dead event; matches={len(matches)}"
                )
            cursor = conn.execute(
                """
                UPDATE deal_feed_events
                SET status='pending', attempts=0, next_attempt_at=?,
                    lease_until=NULL, last_error=NULL, updated_at=?
                WHERE event_key=? AND status='dead'
                """,
                (now, now, matches[0]["event_key"]),
            )
    print(f"revived: {cursor.rowcount}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Laptop Deal Watcher queue admin")
    parser.add_argument(
        "--database",
        default=_db_path(),
        help="SQLite database path (default from deal_watcher.toml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    stats = sub.add_parser("stats", help="show durable queue counts")
    stats.set_defaults(func=cmd_stats)

    dead = sub.add_parser("dead", help="list dead-letter events")
    dead.add_argument("--limit", type=int, default=20)
    dead.set_defaults(func=cmd_dead)

    retry = sub.add_parser("retry-dead", help="return dead event(s) to pending")
    retry.add_argument("event", nargs="?", help="unique event-key prefix")
    retry.add_argument("--all", action="store_true", help="revive all dead events")
    retry.set_defaults(func=cmd_retry_dead)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
