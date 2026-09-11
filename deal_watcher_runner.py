from __future__ import annotations

from dataclasses import replace
import time

from loguru import logger

from deal_watcher import DealWatcherService, load_deal_watcher_config
from deal_watcher.search_profiles import SearchProfile, load_search_profiles
from load_config import load_avito_config
from parser_cls import AvitoParse
from lang import SPFA_PROXY_REQUIRED


class LaptopDealAvitoParse(AvitoParse):
    """Upstream Avito parser with laptop-specific deal intelligence."""

    def __init__(
        self,
        config,
        stop_event=None,
        deal_config_path: str = "deal_watcher.toml",
        profile: SearchProfile | None = None,
    ):
        self.profile = profile
        super().__init__(config=config, stop_event=stop_event)
        self.deal_config = load_deal_watcher_config(deal_config_path)
        self.deal_watcher = (
            DealWatcherService(self.deal_config)
            if self.deal_config.enabled
            else None
        )

        if self.deal_watcher:
            logger.info(
                "Laptop Deal Watcher включён: profile={} mode={} notify_score={}",
                self.profile.name if self.profile else "default",
                self.profile.mode if self.profile else "fast",
                self.deal_config.notify_score,
            )
        else:
            logger.warning(
                "Laptop Deal Watcher выключен в {}. "
                "Работаю как обычный upstream parser.",
                deal_config_path,
            )

    def is_viewed(self, ad) -> bool:
        """Use profile-scoped dedupe when watcher mode is active.

        MARKET seeing a listing must not hide it from FAST, therefore the seen
        key is (profile, avito_id, price), not the upstream global (id, price).
        """
        if self.deal_watcher and self.profile:
            price_detailed = getattr(ad, "priceDetailed", None)
            price = getattr(price_detailed, "value", None)
            avito_id = getattr(ad, "id", None)
            if isinstance(avito_id, int) and isinstance(price, int):
                return self.deal_watcher.store.is_seen(
                    self.profile.name,
                    avito_id,
                    price,
                )
        return super().is_viewed(ad)

    def _AvitoParse__save_viewed(self, ads) -> None:
        """Override upstream private seen writer for profile-scoped dedupe."""
        if self.deal_watcher and self.profile:
            records: list[tuple[int, int]] = []
            for ad in ads:
                price_detailed = getattr(ad, "priceDetailed", None)
                price = getattr(price_detailed, "value", None)
                avito_id = getattr(ad, "id", None)
                if isinstance(avito_id, int) and isinstance(price, int):
                    records.append((avito_id, price))
            self.deal_watcher.store.mark_seen(self.profile.name, records)
            return
        super()._AvitoParse__save_viewed(ads)

    def filter_ads(self, ads):
        filtered_ads = super().filter_ads(ads)
        if not self.deal_watcher or not filtered_ads:
            return filtered_ads

        notify_ads = []
        processed_ads = []
        source_url = self.profile.url if self.profile else None

        for ad in filtered_ads:
            try:
                analysis = self.deal_watcher.analyze_item(
                    ad,
                    source_url=source_url,
                )
                if analysis is None:
                    # FAST fails open to avoid missing a deal. MARKET retries
                    # malformed/unanalysed records on the next cycle.
                    if not self.profile or self.profile.notify:
                        notify_ads.append(ad)
                        processed_ads.append(ad)
                    continue

                processed_ads.append(ad)
                ad.dealAnalysis = analysis
                logger.info(
                    "deal profile={} score={} label={} price={} title={!r}",
                    self.profile.name if self.profile else "default",
                    analysis.score,
                    analysis.label,
                    analysis.price,
                    getattr(ad, "title", ""),
                )

                should_notify = (
                    not self.profile or self.profile.notify
                ) and self.deal_watcher.should_notify(analysis)
                if should_notify:
                    notify_ads.append(ad)
            except Exception as err:
                logger.exception(
                    "Deal analysis failed for ad {}: {}",
                    getattr(ad, "id", None),
                    err,
                )
                if not self.profile or self.profile.notify:
                    # FAST is fail-open and remembers the listing after the
                    # fallback notification. MARKET leaves it unseen to retry.
                    notify_ads.append(ad)
                    processed_ads.append(ad)

        # Low-score and successfully analysed MARKET ads must not be reanalysed
        # every cycle. Price changes still re-enter because price is part of
        # the seen key.
        self._AvitoParse__save_viewed(processed_ads)

        logger.info(
            "Laptop Deal Watcher: profile={} {} candidates -> {} notifications",
            self.profile.name if self.profile else "default",
            len(filtered_ads),
            len(notify_ads),
        )
        return notify_ads


def _build_profile_parser(base_config, profile: SearchProfile):
    profile_config = replace(
        base_config,
        urls=[profile.url],
        count=profile.pages,
        one_time_start=False,
        max_age=(
            profile.max_age_seconds
            if profile.max_age_seconds is not None
            else base_config.max_age
        ),
    )
    return LaptopDealAvitoParse(profile_config, profile=profile)


def _run_profile_scheduler(base_config, profiles: list[SearchProfile]) -> None:
    # MARKET runs first at startup so the first FAST pass can already use the
    # freshly collected baseline.
    profiles = sorted(profiles, key=lambda p: 0 if p.mode == "market" else 1)
    parsers = {
        profile.name: _build_profile_parser(base_config, profile)
        for profile in profiles
    }
    next_run = {profile.name: 0.0 for profile in profiles}

    while True:
        now = time.monotonic()
        ran_any = False

        for profile in profiles:
            if now < next_run[profile.name]:
                continue

            ran_any = True
            logger.info(
                "Запуск профиля {} (mode={}, pages={})",
                profile.name,
                profile.mode,
                profile.pages,
            )
            try:
                parsers[profile.name].parse()
                next_run[profile.name] = (
                    time.monotonic() + profile.interval_seconds
                )
            except Exception as err:
                logger.exception(
                    "Профиль {} завершился ошибкой: {}",
                    profile.name,
                    err,
                )
                retry_in = min(60, profile.interval_seconds)
                next_run[profile.name] = time.monotonic() + retry_in
                logger.warning(
                    "Профиль {} будет повторён через {} сек.",
                    profile.name,
                    retry_in,
                )

        if base_config.one_time_start:
            logger.info("Все поисковые профили обработаны один раз")
            return

        if not ran_any:
            sleep_for = min(next_run.values()) - time.monotonic()
            time.sleep(max(1.0, min(sleep_for, 30.0)))


def _run_legacy_loop(config) -> None:
    """Fallback when deal_searches.toml is absent."""
    while True:
        try:
            parser = LaptopDealAvitoParse(config)
            parser.parse()
            if config.one_time_start:
                logger.info("Парсинг завершён: включён one_time_start")
                break
            logger.info("Цикл завершён. Пауза {} сек", config.pause_general)
            time.sleep(config.pause_general)
        except KeyboardInterrupt:
            logger.info("Остановлено пользователем")
            break
        except Exception as err:
            logger.exception(err)
            logger.error("Ошибка цикла. Повторный запуск через 30 сек.")
            time.sleep(30)


def main() -> None:
    try:
        config = load_avito_config("config.toml")
    except Exception as err:
        logger.error(f"Ошибка загрузки config.toml: {err}")
        raise SystemExit(1)

    if config.use_bypass_api and not (config.proxy_string or "").strip():
        logger.critical(
            f"SPFA не будет работать без прокси. {SPFA_PROXY_REQUIRED}"
        )
        raise SystemExit(1)

    if config.use_bypass_api and not config.proxy_change_url:
        logger.warning(
            "SPFA запущен со статическим прокси. Увеличьте "
            "pause_between_links и pause_general при большом числе блокировок."
        )

    try:
        profiles = load_search_profiles("deal_searches.toml")
    except Exception as err:
        logger.error("Ошибка deal_searches.toml: {}", err)
        raise SystemExit(1)

    if profiles:
        try:
            _run_profile_scheduler(config, profiles)
        except KeyboardInterrupt:
            logger.info("Остановлено пользователем")
    else:
        logger.warning(
            "deal_searches.toml не найден или не содержит активных профилей; "
            "использую urls из config.toml"
        )
        _run_legacy_loop(config)


if __name__ == "__main__":
    main()
