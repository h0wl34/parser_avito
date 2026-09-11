from __future__ import annotations

from typing import Any

from .models import DealAnalysis


def format_rubles(value: int | None) -> str:
    return "—" if value is None else f"{value:,} ₽".replace(",", " ")


def format_deal_text(ad: Any, analysis: DealAnalysis) -> str:
    title = getattr(ad, "title", "") or "Без названия"
    specs = analysis.specs
    lines = [f"{_emoji(analysis.score)} {analysis.score}/100 — {analysis.label}", title]
    if specs.summary(): lines.append(specs.summary())
    lines.append(f"Цена: {format_rubles(analysis.price)}")
    if analysis.market.median_price:
        lines.append(f"Медиана рынка: {format_rubles(analysis.market.median_price)} ({analysis.discount_pct:+.1f}%)")
        lines.append(f"Выборка: {analysis.market.sample_size}, уровень: {analysis.market.comparison_level}")
    elif analysis.absolute_threshold:
        lines.append(f"Порог {specs.gpu}: {format_rubles(analysis.absolute_threshold)}")
    lines.append(f"Состояние: {specs.condition.value}")
    if analysis.price_drop_pct: lines.append(f"Снижение цены: {analysis.price_drop_pct:.1f}%")
    lines.append("⚠️ " + "; ".join(analysis.risk.flags) if analysis.risk.flags else "Риски: явных не найдено")
    if seller := getattr(ad, "sellerId", None): lines.append(f"Продавец: {seller}")
    if ad_id := getattr(ad, "id", None): lines.append(f"https://www.avito.ru/{ad_id}")
    return "\n".join(lines)


def _emoji(score: int) -> str:
    if score >= 90: return "🚨"
    if score >= 80: return "🔥"
    if score >= 70: return "👀"
    return "·"
