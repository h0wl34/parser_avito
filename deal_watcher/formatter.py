from __future__ import annotations

from typing import Any

from integrations.notifications.utils import escape_markdown_v2
from .models import DealAnalysis


def format_rubles(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,} ₽".replace(",", " ")


def _safe_line(text: str) -> str:
    return escape_markdown_v2(text)


def _listing_url(ad: Any) -> str:
    value = str(getattr(ad, "urlPath", "") or "").strip()
    if value.startswith(("https://", "http://")):
        return value
    if value.startswith("/"):
        return f"https://www.avito.ru{value}"
    avito_id = getattr(ad, "id", "")
    return f"https://www.avito.ru/{avito_id}"


def format_deal_markdown(ad: Any, analysis: DealAnalysis) -> str:
    title = getattr(ad, "title", "") or "Без названия"
    specs = analysis.specs
    parts = [
        f"{_emoji(analysis.score)} *{analysis.score}/100 — {escape_markdown_v2(analysis.label)}*",
        f"[{escape_markdown_v2(title)}]({_listing_url(ad)})",
    ]

    summary = specs.summary()
    if summary:
        parts.append(_safe_line(summary))
    parts.append(_safe_line(f"Цена: {format_rubles(analysis.price)}"))

    if analysis.market.median_price:
        parts.append(
            _safe_line(
                f"Медиана рынка: {format_rubles(analysis.market.median_price)} "
                f"({analysis.discount_pct:+.1f}%)"
            )
        )
        parts.append(
            _safe_line(
                f"Выборка: {analysis.market.sample_size}, "
                f"уровень: {analysis.market.comparison_level}"
            )
        )
    elif analysis.absolute_threshold:
        parts.append(
            _safe_line(
                f"Порог {specs.gpu}: {format_rubles(analysis.absolute_threshold)}"
            )
        )

    parts.append(_safe_line(f"Состояние: {specs.condition.value}"))
    if analysis.price_drop_pct:
        parts.append(_safe_line(f"Снижение цены: {analysis.price_drop_pct:.1f}%"))

    if analysis.risk.flags:
        parts.append("⚠️ " + _safe_line("; ".join(analysis.risk.flags)))
    else:
        parts.append(_safe_line("Риски: явных не найдено"))

    seller = getattr(ad, "sellerId", None)
    if seller:
        parts.append(_safe_line(f"Продавец: {seller}"))

    return "\n".join(parts)


def _emoji(score: int) -> str:
    if score >= 90:
        return "🚨"
    if score >= 80:
        return "🔥"
    if score >= 70:
        return "👀"
    return "·"
