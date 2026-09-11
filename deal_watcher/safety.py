from __future__ import annotations

from collections import deque
import random
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
