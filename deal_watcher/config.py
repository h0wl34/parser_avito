from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib


DEFAULT_SERIES_BONUS = {
    "ThinkBook 16+": 15,
    "ThinkBook 16p": 15,
    "ROG Zephyrus": 15,
    "ROG Strix": 12,
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

    SAFE mode is feed-first. Direct Avito polling is explicitly opt-in and is
    treated as a legacy diagnostic source, not as an anti-bot bypass path.
    """

    enabled: bool = True
    require_anonymous: bool = True
    disable_enrichment_requests: bool = True
    stop_on_block: bool = True
    allow_direct_avito_requests: bool = False
    min_profile_interval_seconds: int = 900
    max_requests_per_hour: int = 12
    interval_jitter_ratio: float = 0.20
    startup_spread_seconds: int = 900
    cooldown_after_block_seconds: int = 86_400
    max_cooldown_after_block_seconds: int = 604_800
    block_backoff_multiplier: float = 2.0
    block_statuses: tuple[int, ...] = (403, 429, 439)

    def validate(self) -> None:
        if self.min_profile_interval_seconds < 60:
            raise ValueError("min_profile_interval_seconds must be >= 60")
        if self.max_requests_per_hour < 1:
            raise ValueError("max_requests_per_hour must be >= 1")
        if not 0 <= self.interval_jitter_ratio <= 0.75:
            raise ValueError("interval_jitter_ratio must be between 0 and 0.75")
        if self.startup_spread_seconds < 0:
            raise ValueError("startup_spread_seconds must be >= 0")
        if self.cooldown_after_block_seconds < 300:
            raise ValueError("cooldown_after_block_seconds must be >= 300")
        if self.max_cooldown_after_block_seconds < self.cooldown_after_block_seconds:
            raise ValueError(
                "max_cooldown_after_block_seconds must be >= cooldown_after_block_seconds"
            )
        if self.block_backoff_multiplier < 1.0:
            raise ValueError("block_backoff_multiplier must be >= 1.0")
        if not self.block_statuses:
            raise ValueError("block_statuses must not be empty")


@dataclass(slots=True)
class HealthConfig:
    """Low-noise operational health monitoring and incident escalation."""

    enabled: bool = True
    check_interval_seconds: int = 30
    failure_grace_seconds: int = 120
    consecutive_failures: int = 3
    repeat_alert_seconds: int = 21_600
    worker_heartbeat_timeout_seconds: int = 150
    ingress_heartbeat_timeout_seconds: int = 150
    telegram_probe_seconds: int = 60
    queue_oldest_pending_seconds: int = 600
    queue_pending_warning: int = 25
    dead_letter_critical: int = 1
    disk_free_warning_mb: int = 1024
    disk_free_critical_mb: int = 256
    relay_url_env: str = "DEAL_ALERT_RELAY_URL"
    relay_secret_env: str = "DEAL_ALERT_RELAY_SECRET"

    def validate(self) -> None:
        if self.check_interval_seconds < 10:
            raise ValueError("health.check_interval_seconds must be >= 10")
        if self.failure_grace_seconds < self.check_interval_seconds:
            raise ValueError(
                "health.failure_grace_seconds must be >= check_interval_seconds"
            )
        if self.consecutive_failures < 1:
            raise ValueError("health.consecutive_failures must be >= 1")
        if self.repeat_alert_seconds < 300:
            raise ValueError("health.repeat_alert_seconds must be >= 300")
        if self.worker_heartbeat_timeout_seconds < 30:
            raise ValueError("health.worker_heartbeat_timeout_seconds must be >= 30")
        if self.ingress_heartbeat_timeout_seconds < 30:
            raise ValueError("health.ingress_heartbeat_timeout_seconds must be >= 30")
        if self.telegram_probe_seconds < 30:
            raise ValueError("health.telegram_probe_seconds must be >= 30")
        if self.queue_oldest_pending_seconds < 60:
            raise ValueError("health.queue_oldest_pending_seconds must be >= 60")
        if self.queue_pending_warning < 1:
            raise ValueError("health.queue_pending_warning must be >= 1")
        if self.dead_letter_critical < 1:
            raise ValueError("health.dead_letter_critical must be >= 1")
        if self.disk_free_critical_mb < 64:
            raise ValueError("health.disk_free_critical_mb must be >= 64")
        if self.disk_free_warning_mb <= self.disk_free_critical_mb:
            raise ValueError(
                "health.disk_free_warning_mb must be > disk_free_critical_mb"
            )


@dataclass(slots=True)
class DealWatcherConfig:
    enabled: bool = False
    notify_score: int = 80
    market_window_days: int = 30
    min_market_samples: int = 5
    # A FAST event that sat in our local durable queue longer than this is no
    # longer useful as a time-sensitive alert. It is still analyzed/stored, but
    # delivery is suppressed so a Telegram outage cannot produce an old flood.
    max_fast_event_age_seconds: int = 7_200
    database_path: str = "database.db"
    series_bonus: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SERIES_BONUS))
    gpu_thresholds: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_GPU_THRESHOLDS))
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    health: HealthConfig = field(default_factory=HealthConfig)


def load_deal_watcher_config(path: str | Path = "deal_watcher.toml") -> DealWatcherConfig:
    config_path = Path(path)
    if not config_path.exists():
        return DealWatcherConfig()

    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    section = raw.get("deal_watcher", raw)
    max_fast_event_age_seconds = int(section.get("max_fast_event_age_seconds", 7_200))
    if max_fast_event_age_seconds < 0:
        raise ValueError("max_fast_event_age_seconds must be >= 0")

    config = DealWatcherConfig(
        enabled=bool(section.get("enabled", False)),
        notify_score=int(section.get("notify_score", 80)),
        market_window_days=int(section.get("market_window_days", 30)),
        min_market_samples=int(section.get("min_market_samples", 5)),
        max_fast_event_age_seconds=max_fast_event_age_seconds,
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
            allow_direct_avito_requests=bool(
                safety.get("allow_direct_avito_requests", False)
            ),
            min_profile_interval_seconds=int(
                safety.get("min_profile_interval_seconds", 900)
            ),
            max_requests_per_hour=int(safety.get("max_requests_per_hour", 12)),
            interval_jitter_ratio=float(
                safety.get("interval_jitter_ratio", 0.20)
            ),
            startup_spread_seconds=int(
                safety.get("startup_spread_seconds", 900)
            ),
            cooldown_after_block_seconds=int(
                safety.get("cooldown_after_block_seconds", 86_400)
            ),
            max_cooldown_after_block_seconds=int(
                safety.get("max_cooldown_after_block_seconds", 604_800)
            ),
            block_backoff_multiplier=float(
                safety.get("block_backoff_multiplier", 2.0)
            ),
            block_statuses=statuses,
        )
        config.safety.validate()

    health = raw.get("health", {})
    if isinstance(health, dict):
        defaults = config.health
        config.health = HealthConfig(
            enabled=bool(health.get("enabled", defaults.enabled)),
            check_interval_seconds=int(
                health.get("check_interval_seconds", defaults.check_interval_seconds)
            ),
            failure_grace_seconds=int(
                health.get("failure_grace_seconds", defaults.failure_grace_seconds)
            ),
            consecutive_failures=int(
                health.get("consecutive_failures", defaults.consecutive_failures)
            ),
            repeat_alert_seconds=int(
                health.get("repeat_alert_seconds", defaults.repeat_alert_seconds)
            ),
            worker_heartbeat_timeout_seconds=int(
                health.get(
                    "worker_heartbeat_timeout_seconds",
                    defaults.worker_heartbeat_timeout_seconds,
                )
            ),
            ingress_heartbeat_timeout_seconds=int(
                health.get(
                    "ingress_heartbeat_timeout_seconds",
                    defaults.ingress_heartbeat_timeout_seconds,
                )
            ),
            telegram_probe_seconds=int(
                health.get("telegram_probe_seconds", defaults.telegram_probe_seconds)
            ),
            queue_oldest_pending_seconds=int(
                health.get(
                    "queue_oldest_pending_seconds",
                    defaults.queue_oldest_pending_seconds,
                )
            ),
            queue_pending_warning=int(
                health.get("queue_pending_warning", defaults.queue_pending_warning)
            ),
            dead_letter_critical=int(
                health.get("dead_letter_critical", defaults.dead_letter_critical)
            ),
            disk_free_warning_mb=int(
                health.get("disk_free_warning_mb", defaults.disk_free_warning_mb)
            ),
            disk_free_critical_mb=int(
                health.get("disk_free_critical_mb", defaults.disk_free_critical_mb)
            ),
            relay_url_env=str(
                health.get("relay_url_env", defaults.relay_url_env)
            ),
            relay_secret_env=str(
                health.get("relay_secret_env", defaults.relay_secret_env)
            ),
        )
        config.health.validate()

    return config
