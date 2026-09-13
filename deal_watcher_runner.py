from __future__ import annotations

from dataclasses import replace
import random
import time

from loguru import logger

from deal_watcher import DealWatcherService, load_deal_watcher_config
from deal_watcher.config import SafetyConfig
from deal_watcher.safety import (
    HourlyRequestBudget,
    PersistentBlockCircuit,
    jittered_interval,
    validate_direct_runtime,
)
from deal_watcher.search_profiles import SearchProfile, load_search_profiles
from integrations.notifications.utils import escape_markdown_v2
from load_config import load_avito_config
from parser.http.client import BlockedAccessError
from parser_cls import AvitoParse


class LaptopDealAvitoParse(AvitoParse):
    """Legacy direct Avito source with laptop-specific deal intelligence."""

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
                "Laptop Deal Watcher LEGACY direct source: profile={} mode={} "
                "notify_score={} safe_mode={}",
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
        """Keep explicit access blocks visible to the guarded scheduler."""
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
    cooldown_seconds: float,
) -> None:
    now = time.monotonic()
    resume_at = now + cooldown_seconds
    for profile in profiles:
        spread = random.uniform(0, safety.startup_spread_seconds)
        next_run[profile.name] = max(next_run[profile.name], resume_at + spread)


def _notify_block(
    parser: LaptopDealAvitoParse,
    err: BlockedAccessError,
    cooldown_seconds: float,
) -> None:
    message = (
        f"Avito direct source rejected HTTP {err.status_code}. "
        f"Circuit breaker is open for about {cooldown_seconds / 3600:.1f} hours. "
        "No automatic bypass or proxy rotation will be attempted."
    )
    try:
        parser.notifier.notify(message=escape_markdown_v2(message))
    except Exception as notify_err:
        logger.warning("Не удалось отправить block notification: {}", notify_err)


def _run_profile_scheduler(
    base_config,
    profiles: list[SearchProfile],
    safety: SafetyConfig,
    database_path: str,
) -> None:
    # FAST one-page profiles are the least expensive diagnostic lane. MARKET
    # profiles are intentionally last; production market observations should
    # normally arrive through feed mode instead of direct polling.
    profiles = sorted(profiles, key=lambda p: 0 if p.mode == "fast" else 1)

    run_once = bool(base_config.one_time_start)
    if run_once and safety.enabled:
        diagnostic = next((p for p in profiles if p.mode == "fast"), profiles[0])
        diagnostic = replace(diagnostic, pages=1)
        profiles = [diagnostic]
        logger.warning(
            "SAFE one_time_start is a single-profile canary now: profile={} pages=1",
            diagnostic.name,
        )

    circuit = PersistentBlockCircuit(database_path)
    remaining = circuit.seconds_remaining()
    if safety.enabled and remaining > 0:
        logger.error(
            "Persistent circuit breaker is still open for {:.1f} hours; "
            "direct Avito requests were not attempted. Use deal_feed_runner.py.",
            remaining / 3600,
        )
        return

    parsers = {
        profile.name: _build_profile_parser(base_config, profile, safety)
        for profile in profiles
    }
    effective_profiles = [parsers[p.name].profile for p in profiles]

    completed_once: set[str] = set()
    start = time.monotonic()
    next_run: dict[str, float] = {}
    for index, profile in enumerate(effective_profiles):
        if run_once:
            next_run[profile.name] = 0.0
            continue
        if not safety.enabled:
            next_run[profile.name] = 0.0
            continue

        # No startup burst. Profiles are spread across the startup window and
        # MARKET lanes are shifted behind FAST lanes.
        lane_offset = index * max(1.0, safety.startup_spread_seconds / max(1, len(effective_profiles)))
        if profile.mode == "market":
            lane_offset += safety.startup_spread_seconds
        spread = random.uniform(0, max(1.0, safety.startup_spread_seconds / 4))
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
                remaining = circuit.seconds_remaining()
                if remaining > 0:
                    logger.error(
                        "Circuit breaker reopened externally; {:.1f} hours remain. Exiting direct source.",
                        remaining / 3600,
                    )
                    return

                budget_wait = budget.seconds_until_available(profile.pages, now)
                if budget_wait > 0:
                    extra = random.uniform(30.0, 120.0)
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
                "Запуск LEGACY direct profile={} mode={} pages={}",
                profile.name,
                profile.mode,
                profile.pages,
            )
            try:
                parsers[profile.name].parse()
                if safety.enabled:
                    circuit.record_success()
                if run_once:
                    completed_once.add(profile.name)
                    next_run[profile.name] = float("inf")
                    logger.info("SAFE canary profile={} завершён", profile.name)
                else:
                    delay = jittered_interval(profile, safety)
                    next_run[profile.name] = time.monotonic() + delay
                    logger.info(
                        "Следующий direct profile={} не раньше чем через {:.0f} сек.",
                        profile.name,
                        delay,
                    )
            except BlockedAccessError as err:
                cooldown = (
                    circuit.record_block(safety)
                    if safety.enabled
                    else float(safety.cooldown_after_block_seconds)
                )
                logger.error(
                    "HTTP {} для direct profile={}; persistent circuit breaker: {:.1f} hours.",
                    err.status_code,
                    profile.name,
                    cooldown / 3600,
                )
                _notify_block(parsers[profile.name], err, cooldown)
                _schedule_after_global_block(
                    profiles=effective_profiles,
                    next_run=next_run,
                    safety=safety,
                    cooldown_seconds=cooldown,
                )
                global_block = True
                break
            except Exception as err:
                logger.exception("Direct profile {} завершился ошибкой: {}", profile.name, err)
                # Unknown failures are not retried aggressively. A transient
                # software/network failure should not become a request storm.
                retry_in = max(900.0, float(profile.interval_seconds))
                if safety.enabled:
                    retry_in += random.uniform(60.0, 300.0)
                next_run[profile.name] = time.monotonic() + retry_in
                logger.warning(
                    "Direct profile {} будет повторён не раньше чем через {:.0f} сек.",
                    profile.name,
                    retry_in,
                )

        if run_once and len(completed_once) == len(effective_profiles):
            logger.info("SAFE direct canary completed")
            return

        if global_block:
            # Do not keep a daemon sleeping for a day: persist the state and
            # terminate. A supervisor restart will read the same open circuit.
            return

        if not ran_any:
            sleep_for = min(next_run.values()) - time.monotonic()
            time.sleep(max(1.0, min(sleep_for, 30.0)))


def _run_legacy_loop(config) -> None:
    """Non-SAFE upstream fallback; kept only for compatibility."""
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
            logger.error("Ошибка legacy цикла. Повторный запуск через 30 сек.")
            time.sleep(30)


def main() -> None:
    try:
        config = load_avito_config("config.toml")
        deal_config = load_deal_watcher_config("deal_watcher.toml")
        validate_direct_runtime(config, deal_config.safety)
    except Exception as err:
        logger.error("Direct source refused by configuration: {}", err)
        logger.error("Production path: python deal_feed_runner.py")
        raise SystemExit(2)

    try:
        profiles = load_search_profiles("deal_searches.toml")
    except Exception as err:
        logger.error("Ошибка deal_searches.toml: {}", err)
        raise SystemExit(1)

    if profiles:
        try:
            _run_profile_scheduler(
                config,
                profiles,
                deal_config.safety,
                deal_config.database_path,
            )
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
