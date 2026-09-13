from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import DealWatcherConfig
from .models import DealAnalysis
from .normalizer import assess_risk, extract_specs
from .scoring import analyze_deal
from .storage import DealWatcherStore


class DealWatcherService:
    def __init__(self, config: DealWatcherConfig):
        self.config = config
        self.store = DealWatcherStore(config.database_path)

    def analyze_item(
        self,
        ad: Any,
        source_url: str | None = None,
        baseline_eligible: bool = False,
    ) -> DealAnalysis | None:
        price_detailed = getattr(ad, "priceDetailed", None)
        price = getattr(price_detailed, "value", None)
        avito_id = getattr(ad, "id", None)
        if not isinstance(price, int) or price <= 0 or not isinstance(avito_id, int):
            return None

        title = getattr(ad, "title", "") or ""
        description = getattr(ad, "description", "") or ""
        specs = extract_specs(title, description)
        risk = assess_risk(title, description)

        published_at = None
        timestamp_ms = getattr(ad, "sortTimeStamp", None)
        if isinstance(timestamp_ms, int) and timestamp_ms > 0:
            published_at = datetime.fromtimestamp(
                timestamp_ms / 1000,
                tz=timezone.utc,
            )

        price_drop_pct = self.store.record_listing(
            avito_id=avito_id,
            title=title,
            seller_id=getattr(ad, "sellerId", None),
            url=getattr(ad, "urlPath", None),
            price=price,
            specs=specs,
            risk_score=risk.score,
            source_url=source_url,
            baseline_eligible=baseline_eligible,
            published_at=published_at,
        )
        market = self.store.market_stats(
            specs,
            window_days=self.config.market_window_days,
            min_samples=self.config.min_market_samples,
            exclude_avito_id=avito_id,
        )
        return analyze_deal(
            price=price,
            specs=specs,
            risk=risk,
            market=market,
            config=self.config,
            price_drop_pct=price_drop_pct,
        )

    def should_notify(self, analysis: DealAnalysis | None) -> bool:
        return bool(analysis and analysis.score >= self.config.notify_score)
