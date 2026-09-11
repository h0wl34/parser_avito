from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib


DEFAULT_SERIES_BONUS = {
    "ThinkBook 16+": 15,
    "ThinkBook 16p": 15,
    "ROG Zephyrus": 15,
    "Yoga Pro": 14,
    "ProArt": 14,
    "Legion 7": 12,
    "Legion 5": 10,
    "TUF": 5,
    "Omen": 7,
    "Predator": 5,
    "MSI Katana": -8,
    "MSI Cyborg": -12,
    "MSI Thin": -18,
}

DEFAULT_GPU_THRESHOLDS = {
    "RTX 4080": 125_000,
    "RTX 4070": 100_000,
    "RTX 5070": 125_000,
    "RTX 5060": 110_000,
    "RTX 5050": 90_000,
}


@dataclass(slots=True)
class SafetyConfig:
    """Conservative operating limits for the watcher.

    These controls are intended to reduce request volume and stop cleanly when
    Avito rejects traffic. They are not an anti-bot bypass mechanism.
    """

    enabled: bool = True
    require_anonymous: bool = True
    disable_enrichment_requests: bool = True
    stop_on_block: bool = True
    min_profile_interval_seconds: int = 300
    max_requests_per_hour: int = 120
    interval_jitter_ratio: float = 0.25
    startup_spread_seconds: int = 180
    cooldown_after_block_seconds: int = 21_600
    block_statuses: tuple[int, ...] = (403, 429, 439)

    def validate(self) -> None:
        if self.min_profile_interval_seconds < 30:
            raise ValueError("min_profile_interval_seconds must be >= 30")
        if self.max_requests_per_hour < 1:
            raise ValueError("max_requests_per_hour must be >= 1")
        if not 0 <= self.interval_jitter_ratio <= 0.75:
            raise ValueError("interval_jitter_ratio must be between 0 and 0.75")
        if self.startup_spread_seconds < 0:
            raise ValueError("startup_spread_seconds must be >= 0")
        if self.cooldown_after_block_seconds < 300:
            raise ValueError("cooldown_after_block_seconds must be >= 300")
        if not self.block_statuses:
            raise ValueError("block_statuses must not be empty")


@dataclass(slots=True)
class DealWatcherConfig:
    enabled: bool = False
    notify_score: int = 80
    market_window_days: int = 30
    min_market_samples: int = 5
    database_path: str = "database.db"
    series_bonus: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SERIES_BONUS))
    gpu_thresholds: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_GPU_THRESHOLDS))
    safety: SafetyConfig = field(default_factory=SafetyConfig)


def load_deal_watcher_config(path: str | Path = "deal_watcher.toml") -> DealWatcherConfig:
    config_path = Path(path)
    if not config_path.exists():
        return DealWatcherConfig()

    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    section = raw.get("deal_watcher", raw)
    config = DealWatcherConfig(
        enabled=bool(section.get("enabled", False)),
        notify_score=int(section.get("notify_score", 80)),
        market_window_days=int(section.get("market_window_days", 30)),
        min_market_samples=int(section.get("min_market_samples", 5)),
        database_path=str(section.get("database_path", "database.db")),
    )

    series = raw.get("series_bonus", {})
    if isinstance(series, dict):
        config.series_bonus.update({str(k): int(v) for k, v in series.items()})

    thresholds = raw.get("gpu_thresholds", {})
    if isinstance(thresholds, dict):
        config.gpu_thresholds.update({str(k): int(v) for k, v in thresholds.items()})

    safety = raw.get("safety", {})
    if isinstance(safety, dict):
        raw_statuses = safety.get("block_statuses", config.safety.block_statuses)
        statuses = tuple(int(value) for value in raw_statuses)
        config.safety = SafetyConfig(
            enabled=bool(safety.get("enabled", True)),
            require_anonymous=bool(safety.get("require_anonymous", True)),
            disable_enrichment_requests=bool(
                safety.get("disable_enrichment_requests", True)
            ),
            stop_on_block=bool(safety.get("stop_on_block", True)),
            min_profile_interval_seconds=int(
                safety.get("min_profile_interval_seconds", 300)
            ),
            max_requests_per_hour=int(safety.get("max_requests_per_hour", 120)),
            interval_jitter_ratio=float(
                safety.get("interval_jitter_ratio", 0.25)
            ),
            startup_spread_seconds=int(
                safety.get("startup_spread_seconds", 180)
            ),
            cooldown_after_block_seconds=int(
                safety.get("cooldown_after_block_seconds", 21_600)
            ),
            block_statuses=statuses,
        )
        config.safety.validate()

    return config
