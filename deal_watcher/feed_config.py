from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib


@dataclass(slots=True)
class ImapFeedConfig:
    enabled: bool = False
    host: str = ""
    port: int = 993
    folder: str = "INBOX"
    poll_seconds: int = 60
    sender_contains: str = "avito"
    username_env: str = "AVITO_MAIL_USER"
    password_env: str = "AVITO_MAIL_PASSWORD"
    profile: str = "imap-fast"
    mode: str = "fast"
    mark_seen: bool = True

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.host.strip():
            raise ValueError("imap.host is required when IMAP feed is enabled")
        if self.port < 1 or self.port > 65535:
            raise ValueError("imap.port must be between 1 and 65535")
        if self.poll_seconds < 60:
            raise ValueError("imap.poll_seconds must be >= 60")
        if self.mode not in {"fast", "market"}:
            raise ValueError("imap.mode must be 'fast' or 'market'")
        if not self.username_env.strip() or not self.password_env.strip():
            raise ValueError("imap credential environment variable names are required")


@dataclass(slots=True)
class JsonlFeedConfig:
    enabled: bool = True
    path: str = "deal_feed.jsonl"
    poll_seconds: int = 2
    start_at_end: bool = False

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.path.strip():
            raise ValueError("jsonl.path is required")
        if self.poll_seconds < 1:
            raise ValueError("jsonl.poll_seconds must be >= 1")


@dataclass(slots=True)
class WebhookIngressConfig:
    """Durable ingress for a documented third-party listing webhook."""

    enabled: bool = False
    provider: str = "avigram"
    bind_host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/avigram-callback"
    require_signature: bool = True
    secret_env: str = "AVIGRAM_CALLBACK_SECRET"
    max_skew_seconds: int = 300
    max_body_bytes: int = 1_048_576
    market_name_prefixes: tuple[str, ...] = ("market-", "market_", "market ")

    def validate(self) -> None:
        if not self.enabled:
            return
        if self.provider != "avigram":
            raise ValueError("webhook.provider currently supports only 'avigram'")
        if self.port < 1 or self.port > 65535:
            raise ValueError("webhook.port must be between 1 and 65535")
        if not self.path.startswith("/"):
            raise ValueError("webhook.path must start with '/'")
        if self.require_signature and not self.secret_env.strip():
            raise ValueError("webhook.secret_env is required when signature is required")
        if self.max_skew_seconds < 30 or self.max_skew_seconds > 3600:
            raise ValueError("webhook.max_skew_seconds must be between 30 and 3600")
        if self.max_body_bytes < 1024:
            raise ValueError("webhook.max_body_bytes must be >= 1024")


@dataclass(slots=True)
class FeedSourcesConfig:
    imap: ImapFeedConfig = field(default_factory=ImapFeedConfig)
    jsonl: JsonlFeedConfig = field(default_factory=JsonlFeedConfig)
    webhook: WebhookIngressConfig = field(default_factory=WebhookIngressConfig)

    @property
    def has_enabled_source(self) -> bool:
        return self.imap.enabled or self.jsonl.enabled or self.webhook.enabled

    def validate(self) -> None:
        self.imap.validate()
        self.jsonl.validate()
        self.webhook.validate()
        if not self.has_enabled_source:
            raise ValueError("at least one feed source must be enabled")


def load_feed_sources_config(
    path: str | Path = "deal_sources.toml",
) -> FeedSourcesConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"{config_path} not found; copy deal_sources.toml.example and configure a feed source"
        )

    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    imap_raw = raw.get("imap", {})
    jsonl_raw = raw.get("jsonl", {})
    webhook_raw = raw.get("webhook", {})
    prefixes = webhook_raw.get(
        "market_name_prefixes",
        ["market-", "market_", "market "],
    )
    config = FeedSourcesConfig(
        imap=ImapFeedConfig(
            enabled=bool(imap_raw.get("enabled", False)),
            host=str(imap_raw.get("host", "")),
            port=int(imap_raw.get("port", 993)),
            folder=str(imap_raw.get("folder", "INBOX")),
            poll_seconds=int(imap_raw.get("poll_seconds", 60)),
            sender_contains=str(imap_raw.get("sender_contains", "avito")),
            username_env=str(imap_raw.get("username_env", "AVITO_MAIL_USER")),
            password_env=str(imap_raw.get("password_env", "AVITO_MAIL_PASSWORD")),
            profile=str(imap_raw.get("profile", "imap-fast")),
            mode=str(imap_raw.get("mode", "fast")).lower(),
            mark_seen=bool(imap_raw.get("mark_seen", True)),
        ),
        jsonl=JsonlFeedConfig(
            enabled=bool(jsonl_raw.get("enabled", True)),
            path=str(jsonl_raw.get("path", "deal_feed.jsonl")),
            poll_seconds=int(jsonl_raw.get("poll_seconds", 2)),
            start_at_end=bool(jsonl_raw.get("start_at_end", False)),
        ),
        webhook=WebhookIngressConfig(
            enabled=bool(webhook_raw.get("enabled", False)),
            provider=str(webhook_raw.get("provider", "avigram")).lower(),
            bind_host=str(webhook_raw.get("bind_host", "127.0.0.1")),
            port=int(webhook_raw.get("port", 8765)),
            path=str(webhook_raw.get("path", "/avigram-callback")),
            require_signature=bool(webhook_raw.get("require_signature", True)),
            secret_env=str(
                webhook_raw.get("secret_env", "AVIGRAM_CALLBACK_SECRET")
            ),
            max_skew_seconds=int(webhook_raw.get("max_skew_seconds", 300)),
            max_body_bytes=int(webhook_raw.get("max_body_bytes", 1_048_576)),
            market_name_prefixes=tuple(str(v).lower() for v in prefixes),
        ),
    )
    config.validate()
    return config
