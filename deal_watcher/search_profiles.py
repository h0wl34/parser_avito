from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True, slots=True)
class SearchProfile:
    name: str
    url: str
    mode: str = "fast"
    interval_seconds: int = 180
    pages: int = 1
    max_age_seconds: int | None = None
    enabled: bool = True

    @property
    def notify(self) -> bool:
        return self.mode == "fast"

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("search profile name must not be empty")
        if not self.url.startswith(
            (
                "https://www.avito.ru/",
                "http://www.avito.ru/",
                "https://avito.ru/",
                "http://avito.ru/",
            )
        ):
            raise ValueError(f"{self.name}: expected an Avito URL")
        if self.mode not in {"fast", "market"}:
            raise ValueError(f"{self.name}: unsupported mode {self.mode!r}")
        if self.interval_seconds < 30:
            raise ValueError(f"{self.name}: interval_seconds must be >= 30")
        if self.pages < 1:
            raise ValueError(f"{self.name}: pages must be >= 1")


def load_search_profiles(
    path: str | Path = "deal_searches.toml",
) -> list[SearchProfile]:
    config_path = Path(path)
    if not config_path.exists():
        return []

    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    entries = raw.get("search", [])
    if not isinstance(entries, list):
        raise ValueError("deal_searches.toml: [[search]] array is required")

    profiles: list[SearchProfile] = []
    names: set[str] = set()
    for entry in entries:
        profile = SearchProfile(
            name=str(entry.get("name", "")).strip(),
            url=str(entry.get("url", "")).strip(),
            mode=str(entry.get("mode", "fast")).strip().lower(),
            interval_seconds=int(entry.get("interval_seconds", 180)),
            pages=int(entry.get("pages", 1)),
            max_age_seconds=(
                int(entry["max_age_seconds"])
                if entry.get("max_age_seconds") is not None
                else None
            ),
            enabled=bool(entry.get("enabled", True)),
        )
        profile.validate()
        if profile.name in names:
            raise ValueError(f"duplicate search profile name: {profile.name}")
        names.add(profile.name)
        if profile.enabled:
            profiles.append(profile)

    return profiles
