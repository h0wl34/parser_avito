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
class FeedSourcesConfig:
    imap: ImapFeedConfig = field(default_factory=ImapFeedConfig)
    jsonl: JsonlFeedConfig = field(default_factory=JsonlFeedConfig)

    @property
    def has_enabled_source(self) -> bool:
        return self.imap.enabled or self.jsonl.enabled

    def validate(self) -> None:
        self.imap.validate()
        self.jsonl.validate()
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
    )
    config.validate()
    return config
