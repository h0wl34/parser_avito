from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import time

from loguru import logger

from deal_watcher import load_deal_watcher_config
from deal_watcher.feed_config import load_feed_sources_config
from deal_watcher.health import (
    HealthStore,
    SystemAlertSender,
    disk_free_mb,
    format_duration,
    queue_health_snapshot,
)
from load_config import load_avito_config


def _heartbeat_observation(
    store: HealthStore,
    *,
    service: str,
    timeout_seconds: int,
    now: datetime,
) -> tuple[bool, str]:
    heartbeat = store.heartbeat(service)
    if heartbeat is None:
        return False, f"No heartbeat has ever been recorded for {service}"
    age = max(0.0, (now - heartbeat.updated_at).total_seconds())
    if age > timeout_seconds:
        return (
            False,
            f"{service} heartbeat is stale: {format_duration(age)} old "
            f"(limit {timeout_seconds}s)",
        )
    return True, f"{service} heartbeat healthy ({int(age)}s old)"


def _drain_system_alerts(
    *,
    store: HealthStore,
    sender: SystemAlertSender,
    max_alerts: int = 10,
) -> int:
    delivered = 0
    for _ in range(max_alerts):
        alert = store.claim_alert()
        if alert is None:
            break

        # If an open/repeat alarm could not be delivered while the incident was
        # active and the system has since recovered, do not send a stale red
        # alarm after recovery. The queued RECOVERED event remains useful and
        # carries the incident duration.
        if alert.kind in {"open", "repeat"}:
            active_keys = {
                str(row["incident_key"])
                for row in store.active_incidents()
            }
            if alert.incident_key not in active_keys:
                store.alert_delivered(alert.alert_id)
                logger.info(
                    "system alert superseded after recovery incident={} kind={}",
                    alert.incident_key,
                    alert.kind,
                )
                continue

        ok, detail = sender.send(alert.message)
        if ok:
            store.alert_delivered(alert.alert_id)
            logger.info(
                "system alert delivered incident={} kind={} via={}",
                alert.incident_key,
                alert.kind,
                detail,
            )
            delivered += 1
        else:
            store.alert_retry(
                alert.alert_id,
                error=detail,
                attempts=alert.attempts,
            )
            logger.warning(
                "system alert delivery failed incident={} kind={} attempt={}: {}",
                alert.incident_key,
                alert.kind,
                alert.attempts,
                detail,
            )
            # If both primary and emergency paths are unavailable, immediately
            # retrying all queued alerts only creates noise and network churn.
            break
    return delivered


def _quick_check(db_path: str) -> tuple[bool, str]:
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            result = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
        value = str(result[0]) if result else "no result"
        return value.lower() == "ok", f"SQLite quick_check={value}"
    except Exception as err:
        return False, f"SQLite quick_check failed: {type(err).__name__}"


def run(
    *,
    avito_config_path: str = "config.toml",
    deal_config_path: str = "deal_watcher.toml",
    sources_config_path: str = "deal_sources.toml",
) -> None:
    base = load_avito_config(avito_config_path)
    config = load_deal_watcher_config(deal_config_path)
    sources = load_feed_sources_config(sources_config_path)
    health = config.health
    if not health.enabled:
        raise SystemExit("health.enabled=false in deal_watcher.toml")

    store = HealthStore(config.database_path)
    recovered = store.reset_stuck_alerts()
    if recovered:
        logger.warning("recovered {} system alerts left in sending state", recovered)

    sender = SystemAlertSender(
        bot_token=base.tg_token,
        chat_ids=base.tg_chat_id,
        proxy=base.proxy_notifier,
        relay_url=os.environ.get(health.relay_url_env),
        relay_secret=os.environ.get(health.relay_secret_env),
    )

    logger.info(
        "Health monitor started; interval={}s grace={}s failures={} repeat={}s relay={}",
        health.check_interval_seconds,
        health.failure_grace_seconds,
        health.consecutive_failures,
        health.repeat_alert_seconds,
        "configured" if os.environ.get(health.relay_url_env) else "not-configured",
    )

    last_telegram_probe = 0.0
    last_telegram_result: tuple[bool, str] | None = None
    last_db_check = 0.0
    last_health_log = 0.0

    while True:
        started = time.monotonic()
        now = datetime.now(timezone.utc)
        try:
            store.touch_heartbeat(
                "health_monitor",
                details={"pid": os.getpid()},
                now=now,
            )

            worker_ok, worker_summary = _heartbeat_observation(
                store,
                service="feed_worker",
                timeout_seconds=health.worker_heartbeat_timeout_seconds,
                now=now,
            )
            store.observe(
                "feed_worker",
                healthy=worker_ok,
                severity="CRITICAL",
                summary=worker_summary,
                grace_seconds=health.failure_grace_seconds,
                consecutive_required=health.consecutive_failures,
                repeat_alert_seconds=health.repeat_alert_seconds,
                now=now,
            )

            if sources.webhook.enabled:
                ingress_ok, ingress_summary = _heartbeat_observation(
                    store,
                    service="webhook_ingress",
                    timeout_seconds=health.ingress_heartbeat_timeout_seconds,
                    now=now,
                )
                store.observe(
                    "webhook_ingress",
                    healthy=ingress_ok,
                    severity="CRITICAL",
                    summary=ingress_summary,
                    grace_seconds=health.failure_grace_seconds,
                    consecutive_required=health.consecutive_failures,
                    repeat_alert_seconds=health.repeat_alert_seconds,
                    now=now,
                )

            snapshot = queue_health_snapshot(config.database_path, now=now)
            store.observe(
                "dead_letter",
                healthy=snapshot.dead < health.dead_letter_critical,
                severity="CRITICAL",
                summary=(
                    f"Dead-letter queue contains {snapshot.dead} event(s); "
                    "manual inspection/retry is required"
                    if snapshot.dead >= health.dead_letter_critical
                    else "Dead-letter queue is empty"
                ),
                grace_seconds=0,
                consecutive_required=1,
                repeat_alert_seconds=health.repeat_alert_seconds,
                now=now,
            )

            oldest = snapshot.oldest_unfinished_age_seconds
            queue_stalled = (
                oldest is not None
                and oldest > health.queue_oldest_pending_seconds
                and (snapshot.pending + snapshot.processing) > 0
            )
            store.observe(
                "queue_stalled",
                healthy=not queue_stalled,
                severity="CRITICAL",
                summary=(
                    f"Oldest unfinished feed event is {format_duration(oldest or 0)} old; "
                    f"pending={snapshot.pending}, processing={snapshot.processing}"
                    if queue_stalled
                    else "Feed queue latency is within limits"
                ),
                grace_seconds=health.check_interval_seconds,
                consecutive_required=2,
                repeat_alert_seconds=health.repeat_alert_seconds,
                now=now,
            )

            queue_large = snapshot.pending >= health.queue_pending_warning
            store.observe(
                "queue_backlog",
                healthy=not queue_large,
                severity="WARNING",
                summary=(
                    f"Feed queue backlog is {snapshot.pending} pending event(s)"
                    if queue_large
                    else "Feed queue backlog is normal"
                ),
                grace_seconds=max(300, health.failure_grace_seconds),
                consecutive_required=max(3, health.consecutive_failures),
                repeat_alert_seconds=health.repeat_alert_seconds,
                now=now,
            )

            free_mb = disk_free_mb(Path(config.database_path).parent or Path("."))
            disk_bad = free_mb < health.disk_free_warning_mb
            disk_severity = (
                "CRITICAL" if free_mb < health.disk_free_critical_mb else "WARNING"
            )
            store.observe(
                "disk_space",
                healthy=not disk_bad,
                severity=disk_severity,
                summary=(
                    f"Only {free_mb} MiB free on watcher filesystem"
                    if disk_bad
                    else f"Disk free space healthy: {free_mb} MiB"
                ),
                grace_seconds=health.failure_grace_seconds,
                consecutive_required=health.consecutive_failures,
                repeat_alert_seconds=health.repeat_alert_seconds,
                now=now,
            )

            mono = time.monotonic()
            if mono - last_telegram_probe >= health.telegram_probe_seconds:
                last_telegram_result = sender.probe_primary_telegram()
                last_telegram_probe = mono
                tg_ok, tg_summary = last_telegram_result
                store.observe(
                    "telegram_path",
                    healthy=tg_ok,
                    severity="CRITICAL",
                    summary=tg_summary,
                    grace_seconds=health.failure_grace_seconds,
                    consecutive_required=health.consecutive_failures,
                    repeat_alert_seconds=health.repeat_alert_seconds,
                    now=now,
                )

            if mono - last_db_check >= 3600:
                db_ok, db_summary = _quick_check(config.database_path)
                store.observe(
                    "database_integrity",
                    healthy=db_ok,
                    severity="CRITICAL",
                    summary=db_summary,
                    grace_seconds=0,
                    consecutive_required=1,
                    repeat_alert_seconds=health.repeat_alert_seconds,
                    now=now,
                )
                last_db_check = mono

            _drain_system_alerts(store=store, sender=sender)

            if mono - last_health_log >= 300:
                logger.info(
                    "health ok; queue={} active_incidents={}",
                    snapshot,
                    len(store.active_incidents()),
                )
                last_health_log = mono

        except KeyboardInterrupt:
            raise
        except Exception:
            # systemd should keep this process alive. We log the failure here;
            # if the DB itself is broken the external emergency relay/watchdog
            # remains the only reliable out-of-process path.
            logger.exception("health iteration failed")

        elapsed = time.monotonic() - started
        time.sleep(max(1.0, health.check_interval_seconds - elapsed))


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        logger.info("Health monitor stopped")
