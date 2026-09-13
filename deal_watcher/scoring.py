from __future__ import annotations

from .config import DealWatcherConfig
from .models import Condition, DealAnalysis, LaptopSpecs, MarketStats, RiskAssessment

_CONDITION_POINTS = {
    Condition.NEW_CONFIRMED: 10,
    Condition.NEW_LIKELY: 7,
    Condition.LIKE_NEW: 4,
    Condition.UNKNOWN: 2,
    Condition.USED: -8,
    Condition.REFURBISHED: -18,
    Condition.BROKEN: -40,
}


def _series_bonus(specs: LaptopSpecs, config: DealWatcherConfig) -> int:
    return config.series_bonus.get(specs.family, 0) if specs.family else 0


def _absolute_value_score(price: int, specs: LaptopSpecs, config: DealWatcherConfig) -> tuple[int, int | None]:
    if not specs.gpu:
        return 0, None
    threshold = config.gpu_thresholds.get(specs.gpu)
    if not threshold:
        return 0, None
    ratio = price / threshold
    if ratio <= 0.90: return 15, threshold
    if ratio <= 1.00: return 12, threshold
    if ratio <= 1.10: return 7, threshold
    if ratio <= 1.20: return 3, threshold
    return 0, threshold


def _market_score(price: int, market: MarketStats) -> tuple[int, float | None]:
    if not market.median_price:
        return 0, None
    discount = (market.median_price - price) / market.median_price * 100
    if discount >= 30: return 40, discount
    if discount >= 25: return 36, discount
    if discount >= 20: return 31, discount
    if discount >= 15: return 25, discount
    if discount >= 10: return 18, discount
    if discount >= 5: return 10, discount
    if discount >= 0: return 4, discount
    return 0, discount


def _hardware_score(specs: LaptopSpecs) -> int:
    points = 5 if specs.gpu in {"RTX 4080", "RTX 5070", "RTX 4070", "RTX 5060"} else (2 if specs.gpu else 0)
    points += 3 if specs.ram_gb and specs.ram_gb >= 32 else (1 if specs.ram_gb and specs.ram_gb >= 16 else 0)
    points += 2 if specs.storage_gb and specs.storage_gb >= 1024 else 0
    return min(points, 10)


def label_for_score(score: int) -> str:
    if score >= 90: return "IMMEDIATE"
    if score >= 80: return "STRONG"
    if score >= 70: return "INTERESTING"
    return "IGNORE"


def analyze_deal(*, price: int, specs: LaptopSpecs, risk: RiskAssessment, market: MarketStats, config: DealWatcherConfig, price_drop_pct: float | None = None) -> DealAnalysis:
    market_points, discount_pct = _market_score(price, market)
    absolute_points, threshold = _absolute_value_score(price, specs, config)
    series_points = _series_bonus(specs, config)
    score = 20 + market_points + absolute_points + series_points + _CONDITION_POINTS[specs.condition] + _hardware_score(specs)
    if price_drop_pct and price_drop_pct >= 10:
        score += min(8, round(price_drop_pct / 2))
    score -= risk.score
    score = max(0, min(100, round(score)))
    reasons = []
    if discount_pct is not None: reasons.append(f"{discount_pct:+.1f}% к медиане рынка")
    if threshold: reasons.append(f"ориентир {specs.gpu}: {threshold:,} ₽".replace(",", " "))
    if series_points: reasons.append(f"серия {specs.family}: {series_points:+d}")
    if specs.condition == Condition.LIKE_NEW: reasons.append("почти новый: минимальное использование")
    if risk.flags: reasons.append("риски: " + ", ".join(risk.flags))
    if price_drop_pct: reasons.append(f"снижение цены: {price_drop_pct:.1f}%")
    return DealAnalysis(score=score, label=label_for_score(score), specs=specs, risk=risk, market=market, price=price, discount_pct=discount_pct, absolute_threshold=threshold, reasons=reasons, price_drop_pct=price_drop_pct)
