from __future__ import annotations

from collections import deque
import random
import sqlite3
import time
from typing import Any

from .config import SafetyConfig
from .search_profiles import SearchProfile


class HourlyRequestBudget:
    """Conservative rolling budget based on expected catalog page requests."""

    def __init__(self, limit: int):
        self.limit = int(limit)
        self.events: deque[float] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - 3600.0
        while self.events and self.events[0] <= cutoff:
            self.events.popleft()

    def seconds_until_available(self, units: int, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        self._prune(now)
        if len(self.events) + units <= self.limit:
            return 0.0
        required_to_expire = len(self.events) + units - self.limit
        release_at = self.events[required_to_expire - 1] + 3600.0
        return max(0.0, release_at - now)

    def consume(self, units: int, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._prune(now)
        for _ in range(max(0, units)):
            self.events.append(now)


class PersistentBlockCircuit:
    """Block cooldown that survives restarts.

    This is intentionally defensive: restarting the watcher cannot erase a
    remote rejection and immediately generate another request. Repeated blocks
    use exponential backoff capped by SafetyConfig.
    """

    def __init__(self, database_path: str, key: str = "direct-avito"):
        self.database_path = database_path
        self.key = key
        with sqlite3.connect(self.database_path) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS deal_runtime_state (
                    state_key TEXT PRIMARY KEY,
                    blocked_until REAL NOT NULL DEFAULT 0,
                    block_count INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            db.commit()

    def _state(self) -> tuple[float, int]:
        with sqlite3.connect(self.database_path) as db:
            row = db.execute(
                "SELECT blocked_until, block_count FROM deal_runtime_state WHERE state_key = ?",
                (self.key,),
            ).fetchone()
        if not row:
            return 0.0, 0
        return float(row[0]), int(row[1])

    def seconds_remaining(self, now: float | None = None) -> float:
        now = time.time() if now is None else float(now)
        blocked_until, _ = self._state()
        return max(0.0, blocked_until - now)

    def is_open(self, now: float | None = None) -> bool:
        return self.seconds_remaining(now=now) > 0

    def record_block(
        self,
        safety: SafetyConfig,
        *,
        now: float | None = None,
    ) -> float:
        now = time.time() if now is None else float(now)
        _, current_count = self._state()
        new_count = current_count + 1
        cooldown = float(safety.cooldown_after_block_seconds) * (
            float(safety.block_backoff_multiplier) ** max(0, new_count - 1)
        )
        cooldown = min(cooldown, float(safety.max_cooldown_after_block_seconds))
        blocked_until = now + cooldown
        with sqlite3.connect(self.database_path) as db:
            db.execute(
                """
                INSERT INTO deal_runtime_state(state_key, blocked_until, block_count, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    blocked_until = excluded.blocked_until,
                    block_count = excluded.block_count,
                    updated_at = excluded.updated_at
                """,
                (self.key, blocked_until, new_count, now),
            )
            db.commit()
        return cooldown

    def record_success(self, *, now: float | None = None) -> None:
        """Decay history after a completed successful profile; don't erase it."""
        now = time.time() if now is None else float(now)
        _, current_count = self._state()
        new_count = max(0, current_count - 1)
        with sqlite3.connect(self.database_path) as db:
            db.execute(
                """
                INSERT INTO deal_runtime_state(state_key, blocked_until, block_count, updated_at)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    blocked_until = 0,
                    block_count = excluded.block_count,
                    updated_at = excluded.updated_at
                """,
                (self.key, new_count, now),
            )
            db.commit()


def validate_safe_runtime(base_config: Any, safety: SafetyConfig) -> None:
    if not safety.enabled:
        return

    problems: list[str] = []
    if safety.require_anonymous:
        if getattr(base_config, "use_own_cookies", False):
            problems.append("use_own_cookies must be false")
        if getattr(base_config, "use_bypass_api", False):
            problems.append("use_bypass_api must be false")
        if getattr(base_config, "proxy_change_url", None):
            problems.append("proxy_change_url must be empty in safe mode")

    if safety.disable_enrichment_requests:
        if getattr(base_config, "parse_views", False):
            problems.append("parse_views must be false")
        if getattr(base_config, "parse_phone", False):
            problems.append("parse_phone must be false")

    if problems:
        joined = "; ".join(problems)
        raise ValueError(f"SAFE MODE refused to start: {joined}")


def validate_direct_runtime(base_config: Any, safety: SafetyConfig) -> None:
    validate_safe_runtime(base_config, safety)
    if safety.enabled and not safety.allow_direct_avito_requests:
        raise ValueError(
            "SAFE MODE blocks direct Avito polling by default. "
            "Use deal_feed_runner.py for production acquisition. "
            "Set safety.allow_direct_avito_requests=true only for an explicit "
            "legacy diagnostic run."
        )


def jittered_interval(profile: SearchProfile, safety: SafetyConfig) -> float:
    base = float(profile.interval_seconds)
    if not safety.enabled:
        return base
    base = max(base, float(safety.min_profile_interval_seconds))
    spread = base * safety.interval_jitter_ratio
    return max(
        float(safety.min_profile_interval_seconds),
        random.uniform(base - spread, base + spread),
    )
