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
class DealWatcherConfig:
    enabled: bool = False
    notify_score: int = 80
    market_window_days: int = 30
    min_market_samples: int = 5
    database_path: str = "database.db"
    series_bonus: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SERIES_BONUS))
    gpu_thresholds: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_GPU_THRESHOLDS))


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

    return config
