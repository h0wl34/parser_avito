from __future__ import annotations

from dataclasses import replace
import random
import time

from loguru import logger

from deal_watcher import DealWatcherService, load_deal_watcher_config
from deal_watcher.config import SafetyConfig
from deal_watcher.safety import (
    HourlyRequestBudget,
    jittered_interval,
    validate_safe_runtime,
)
from deal_watcher.search_profiles import SearchProfile, load_search_profiles
from integrations.notifications.utils import escape_markdown_v2
from load_config import load_avito_config
from parser.http.client import BlockedAccessError
from parser_cls import AvitoParse


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

        safety = self.deal_config.safety
        if safety.enabled:
            self.http.stop_on_block = safety.stop_on_block
            self.http.block_statuses = tuple(safety.block_statuses)

        if self.deal_watcher:
            logger.info(
                "Laptop Deal Watcher включён: profile={} mode={} notify_score={} safe_mode={}",
                self.profile.name if self.profile else "default",
                self.profile.mode if self.profile else "fast",
                self.deal_config.notify_score,
                safety.enabled,
            )
        else:
            logger.warning(
                "Laptop Deal Watcher выключен в {}. "
                "Работаю как обычный upstream parser.",
                deal_config_path,
            )

    def fetch_api_data(self, api_url: str, page: int) -> dict | None:
        """Keep explicit access blocks visible to the safe scheduler."""
        if self.stop_event and self.stop_event.is_set():
            return None

        page_url = self._api_url_for_page(api_url, page)
        try:
            response = self.http.request("GET", page_url)
            self.good_request_count += 1
            return response.json()
        except BlockedAccessError:
            self.bad_request_count += 1
            raise
        except Exception as err:
            self.bad_request_count += 1
            logger.warning(f"Ошибка при запросе API {page_url}: {err}")
            return None

    def is_viewed(self, ad) -> bool:
        """Use profile-scoped dedupe when watcher mode is active."""
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
        baseline_eligible = bool(self.profile and self.profile.mode == "market")

        for ad in filtered_ads:
            try:
                analysis = self.deal_watcher.analyze_item(
                    ad,
                    source_url=source_url,
                    baseline_eligible=baseline_eligible,
                )
                if analysis is None:
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
                    notify_ads.append(ad)
                    processed_ads.append(ad)

        self._AvitoParse__save_viewed(processed_ads)

        logger.info(
            "Laptop Deal Watcher: profile={} {} candidates -> {} notifications",
            self.profile.name if self.profile else "default",
            len(filtered_ads),
            len(notify_ads),
        )
        return notify_ads


def _build_profile_parser(
    base_config,
    profile: SearchProfile,
    safety: SafetyConfig,
):
    interval = profile.interval_seconds
    if safety.enabled:
        interval = max(interval, safety.min_profile_interval_seconds)

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
        max_count_of_retry=(1 if safety.enabled else base_config.max_count_of_retry),
    )
    effective_profile = replace(profile, interval_seconds=interval)
    return LaptopDealAvitoParse(profile_config, profile=effective_profile)


def _schedule_after_global_block(
    *,
    profiles: list[SearchProfile],
    next_run: dict[str, float],
    safety: SafetyConfig,
) -> float:
    now = time.monotonic()
    resume_at = now + safety.cooldown_after_block_seconds
    for profile in profiles:
        spread = random.uniform(0, safety.startup_spread_seconds)
        next_run[profile.name] = max(next_run[profile.name], resume_at + spread)
    return resume_at


def _notify_block(
    parser: LaptopDealAvitoParse,
    err: BlockedAccessError,
    safety: SafetyConfig,
) -> None:
    message = (
        f"Avito access blocked HTTP {err.status_code}. "
        f"SAFE MODE pauses all searches for {safety.cooldown_after_block_seconds} seconds"
    )
    try:
        parser.notifier.notify(message=escape_markdown_v2(message))
    except Exception as notify_err:
        logger.warning("Не удалось отправить block notification: {}", notify_err)


def _run_profile_scheduler(
    base_config,
    profiles: list[SearchProfile],
    safety: SafetyConfig,
) -> None:
    profiles = sorted(profiles, key=lambda p: 0 if p.mode == "market" else 1)
    parsers = {
        profile.name: _build_profile_parser(base_config, profile, safety)
        for profile in profiles
    }
    effective_profiles = [parsers[p.name].profile for p in profiles]

    run_once = bool(base_config.one_time_start)
    completed_once: set[str] = set()
    start = time.monotonic()
    next_run: dict[str, float] = {}
    for index, profile in enumerate(effective_profiles):
        if run_once or not safety.enabled:
            next_run[profile.name] = 0.0
            continue

        lane_offset = (
            0
            if profile.mode == "market"
            else safety.startup_spread_seconds
        )
        spread = random.uniform(0, safety.startup_spread_seconds)
        if profile.mode == "market" and index == 0:
            spread = 0.0
        next_run[profile.name] = start + lane_offset + spread

    budget = HourlyRequestBudget(safety.max_requests_per_hour)

    while True:
        now = time.monotonic()
        ran_any = False
        global_block = False

        for profile in effective_profiles:
            if run_once and profile.name in completed_once:
                continue
            if now < next_run[profile.name]:
                continue

            if safety.enabled:
                budget_wait = budget.seconds_until_available(profile.pages, now)
                if budget_wait > 0:
                    extra = random.uniform(5.0, 30.0)
                    next_run[profile.name] = now + budget_wait + extra
                    logger.warning(
                        "Часовой бюджет запросов достигнут; profile={} отложен на {:.0f} сек.",
                        profile.name,
                        budget_wait + extra,
                    )
                    continue
                budget.consume(profile.pages, now)

            ran_any = True
            logger.info(
                "Запуск профиля {} (mode={}, pages={})",
                profile.name,
                profile.mode,
                profile.pages,
            )
            try:
                parsers[profile.name].parse()
                if run_once:
                    completed_once.add(profile.name)
                    next_run[profile.name] = float("inf")
                    logger.info(
                        "Одноразовый тест profile={} завершён ({}/{})",
                        profile.name,
                        len(completed_once),
                        len(effective_profiles),
                    )
                else:
                    delay = jittered_interval(profile, safety)
                    next_run[profile.name] = time.monotonic() + delay
                    logger.info(
                        "Следующий запуск profile={} примерно через {:.0f} сек.",
                        profile.name,
                        delay,
                    )
            except BlockedAccessError as err:
                logger.error(
                    "SAFE MODE: HTTP {} для profile={}; все профили уходят в cooldown.",
                    err.status_code,
                    profile.name,
                )
                _notify_block(parsers[profile.name], err, safety)
                _schedule_after_global_block(
                    profiles=effective_profiles,
                    next_run=next_run,
                    safety=safety,
                )
                global_block = True
                break
            except Exception as err:
                logger.exception(
                    "Профиль {} завершился ошибкой: {}",
                    profile.name,
                    err,
                )
                retry_base = min(300, profile.interval_seconds)
                retry_jitter = random.uniform(0, 60) if safety.enabled else 0
                retry_in = retry_base + retry_jitter
                next_run[profile.name] = time.monotonic() + retry_in
                logger.warning(
                    "Профиль {} будет повторён через {:.0f} сек.",
                    profile.name,
                    retry_in,
                )

        if run_once and len(completed_once) == len(effective_profiles):
            logger.info("Все поисковые профили обработаны один раз")
            return

        if global_block:
            time.sleep(30.0)
            continue

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
        deal_config = load_deal_watcher_config("deal_watcher.toml")
        validate_safe_runtime(config, deal_config.safety)
    except Exception as err:
        logger.error("Ошибка конфигурации: {}", err)
        raise SystemExit(1)

    try:
        profiles = load_search_profiles("deal_searches.toml")
    except Exception as err:
        logger.error("Ошибка deal_searches.toml: {}", err)
        raise SystemExit(1)

    if profiles:
        try:
            _run_profile_scheduler(config, profiles, deal_config.safety)
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
