from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Condition(StrEnum):
    NEW_CONFIRMED = "NEW_CONFIRMED"
    NEW_LIKELY = "NEW_LIKELY"
    LIKE_NEW = "LIKE_NEW"
    UNKNOWN = "UNKNOWN"
    USED = "USED"
    REFURBISHED = "REFURBISHED"
    BROKEN = "BROKEN"


@dataclass(slots=True)
class LaptopSpecs:
    brand: str | None = None
    family: str | None = None
    sku: str | None = None
    cpu: str | None = None
    gpu: str | None = None
    ram_gb: int | None = None
    storage_gb: int | None = None
    condition: Condition = Condition.UNKNOWN

    @property
    def market_key_candidates(self) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        if self.sku and self.gpu:
            candidates.append(("sku_gpu", f"{self.sku}|{self.gpu}"))
        if self.family and self.gpu:
            candidates.append(("family_gpu", f"{self.family}|{self.gpu}"))
        if self.brand and self.gpu:
            candidates.append(("brand_gpu", f"{self.brand}|{self.gpu}"))
        if self.gpu:
            candidates.append(("gpu", self.gpu))
        return candidates

    def summary(self) -> str:
        parts = [p for p in (self.gpu, self.cpu) if p]
        if self.ram_gb:
            parts.append(f"{self.ram_gb}GB RAM")
        if self.storage_gb:
            tb = self.storage_gb / 1024
            parts.append(f"{tb:g}TB SSD" if self.storage_gb >= 1024 else f"{self.storage_gb}GB SSD")
        return " • ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["condition"] = self.condition.value
        return data


@dataclass(slots=True)
class RiskAssessment:
    score: int = 0
    flags: list[str] = field(default_factory=list)

    def add(self, penalty: int, flag: str) -> None:
        self.score += max(0, penalty)
        if flag not in self.flags:
            self.flags.append(flag)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MarketStats:
    sample_size: int = 0
    median_price: int | None = None
    p25_price: int | None = None
    p10_price: int | None = None
    min_price: int | None = None
    comparison_level: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DealAnalysis:
    score: int
    label: str
    specs: LaptopSpecs
    risk: RiskAssessment
    market: MarketStats
    price: int
    discount_pct: float | None = None
    absolute_threshold: int | None = None
    reasons: list[str] = field(default_factory=list)
    price_drop_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "label": self.label,
            "specs": self.specs.to_dict(),
            "risk": self.risk.to_dict(),
            "market": self.market.to_dict(),
            "price": self.price,
            "discount_pct": self.discount_pct,
            "absolute_threshold": self.absolute_threshold,
            "reasons": list(self.reasons),
            "price_drop_pct": self.price_drop_pct,
        }
