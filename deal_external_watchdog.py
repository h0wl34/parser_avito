from __future__ import annotations

import os
import time

import requests
from loguru import logger


def _chat_ids() -> list[str]:
    raw = os.environ.get("DEAL_RELAY_CHAT_ID", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _send_telegram(bot_token: str, chat_ids: list[str], text: str) -> bool:
    delivered = False
    for chat_id in chat_ids:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
            delivered = delivered or response.status_code == 200
        except requests.RequestException as err:
            logger.warning("watchdog Telegram send failed: {}", type(err).__name__)
    return delivered


def run() -> None:
    url = os.environ.get("DEAL_WATCHDOG_URL", "").strip()
    bot_token = os.environ.get("DEAL_RELAY_BOT_TOKEN", "").strip()
    chat_ids = _chat_ids()
    interval = max(15, int(os.environ.get("DEAL_WATCHDOG_INTERVAL", "30")))
    failures_required = max(2, int(os.environ.get("DEAL_WATCHDOG_FAILURES", "3")))
    repeat_seconds = max(900, int(os.environ.get("DEAL_WATCHDOG_REPEAT", "21600")))

    if not url:
        raise SystemExit("DEAL_WATCHDOG_URL is required")
    if not bot_token or not chat_ids:
        raise SystemExit("DEAL_RELAY_BOT_TOKEN and DEAL_RELAY_CHAT_ID are required")

    logger.info(
        "External watchdog started url={} interval={}s failures={} repeat={}s",
        url,
        interval,
        failures_required,
        repeat_seconds,
    )

    consecutive_failures = 0
    incident_open = False
    incident_started = 0.0
    last_notified = 0.0

    while True:
        healthy = False
        detail = "unknown"
        try:
            response = requests.get(url, timeout=10)
            healthy = 200 <= response.status_code < 300
            detail = f"HTTP {response.status_code}"
        except requests.RequestException as err:
            detail = type(err).__name__

        now = time.monotonic()
        if healthy:
            consecutive_failures = 0
            if incident_open:
                duration = max(0, int(now - incident_started))
                _send_telegram(
                    bot_token,
                    chat_ids,
                    f"✅ RECOVERED [external_watchdog] watcher endpoint is healthy again. "
                    f"Downtime: {duration}s.",
                )
                incident_open = False
                incident_started = 0.0
                last_notified = 0.0
        else:
            consecutive_failures += 1
            if not incident_open and consecutive_failures >= failures_required:
                incident_open = True
                incident_started = now
                last_notified = now
                _send_telegram(
                    bot_token,
                    chat_ids,
                    f"🚨 CRITICAL [external_watchdog] watcher endpoint unavailable: {detail}. "
                    f"Failed {consecutive_failures} consecutive checks.",
                )
            elif incident_open and now - last_notified >= repeat_seconds:
                last_notified = now
                _send_telegram(
                    bot_token,
                    chat_ids,
                    f"🚨 STILL CRITICAL [external_watchdog] watcher endpoint remains unavailable: {detail}.",
                )

        time.sleep(interval)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        logger.info("External watchdog stopped")
