from __future__ import annotations

import time

from loguru import logger

from deal_watcher import DealWatcherService, load_deal_watcher_config
from load_config import load_avito_config
from parser_cls import AvitoParse
from lang import SPFA_PROXY_REQUIRED


class LaptopDealAvitoParse(AvitoParse):
    """Upstream Avito parser with laptop-specific deal intelligence.

    Only the post-filter stage is extended. Networking, cookies, proxies,
    retries and Avito parsing stay in the upstream implementation.
    """

    def __init__(
        self,
        config,
        stop_event=None,
        deal_config_path: str = "deal_watcher.toml",
    ):
        super().__init__(config=config, stop_event=stop_event)
        self.deal_config = load_deal_watcher_config(deal_config_path)
        self.deal_watcher = (
            DealWatcherService(self.deal_config)
            if self.deal_config.enabled
            else None
        )

        if self.deal_watcher:
            logger.info(
                "Laptop Deal Watcher включён: порог уведомления {}",
                self.deal_config.notify_score,
            )
        else:
            logger.warning(
                "Laptop Deal Watcher выключен в {}. "
                "Работаю как обычный upstream parser.",
                deal_config_path,
            )

    def filter_ads(self, ads):
        filtered_ads = super().filter_ads(ads)
        if not self.deal_watcher or not filtered_ads:
            return filtered_ads

        notify_ads = []
        for ad in filtered_ads:
            try:
                analysis = self.deal_watcher.analyze_item(ad)
                if analysis is None:
                    # Fail open: malformed/changed Avito payload should not
                    # silently hide a potentially valuable listing.
                    notify_ads.append(ad)
                    continue

                ad.dealAnalysis = analysis
                logger.info(
                    "deal score={} label={} price={} title={!r}",
                    analysis.score,
                    analysis.label,
                    analysis.price,
                    getattr(ad, "title", ""),
                )
                if self.deal_watcher.should_notify(analysis):
                    notify_ads.append(ad)
            except Exception as err:
                logger.exception(
                    "Deal analysis failed for ad {}: {}. "
                    "Fail-open notification is used.",
                    getattr(ad, "id", None),
                    err,
                )
                notify_ads.append(ad)

        # Upstream normally marks only ads returned by filter_ads as viewed.
        # In watcher mode low-score ads must also be remembered; otherwise they
        # would be re-analysed every polling cycle. Upstream's viewed key is
        # (id, price), so a price change still enters the pipeline again.
        self._AvitoParse__save_viewed(filtered_ads)

        logger.info(
            "Laptop Deal Watcher: {} candidates -> {} notifications",
            len(filtered_ads),
            len(notify_ads),
        )
        return notify_ads


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


if __name__ == "__main__":
    main()
