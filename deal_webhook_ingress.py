from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
from typing import Type

from loguru import logger

from deal_watcher import load_deal_watcher_config
from deal_watcher.feed_config import FeedSourcesConfig, load_feed_sources_config
from deal_watcher.feed_queue import FeedEventQueue
from deal_watcher.health import HealthStore
from deal_watcher.webhook import (
    candidate_from_avigram_payload,
    verify_avigram_signature,
)


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_handler(
    config: FeedSourcesConfig,
    queue: FeedEventQueue,
    health_store: HealthStore | None = None,
    *,
    health_monitor_timeout_seconds: int = 300,
) -> Type[BaseHTTPRequestHandler]:
    webhook = config.webhook
    secret = os.environ.get(webhook.secret_env, "")
    if webhook.require_signature and not secret:
        raise ValueError(
            f"Webhook signature is required; set environment variable {webhook.secret_env}"
        )
    if not webhook.require_signature:
        logger.warning(
            "Webhook signature verification is disabled. Enable require_signature before exposing ingress."
        )

    class Handler(BaseHTTPRequestHandler):
        server_version = "DealWatcherIngress/2.3"

        def _send(self, status: int, payload: dict) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                status_code = 200
                payload = {
                    "status": "ok",
                    "ingress": webhook.provider,
                    "queue": queue.stats(),
                }
                if health_store is not None:
                    heartbeat = health_store.heartbeat("health_monitor")
                    if heartbeat is None:
                        status_code = 503
                        payload["status"] = "degraded"
                        payload["health_monitor"] = "missing"
                    else:
                        age = max(
                            0.0,
                            (
                                datetime.now(timezone.utc) - heartbeat.updated_at
                            ).total_seconds(),
                        )
                        payload["health_monitor_age_seconds"] = round(age)
                        if age > health_monitor_timeout_seconds:
                            status_code = 503
                            payload["status"] = "degraded"
                            payload["health_monitor"] = "stale"
                        else:
                            payload["health_monitor"] = "ok"
                self._send(status_code, payload)
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != webhook.path:
                self._send(404, {"error": "not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(400, {"error": "invalid content length"})
                return
            if length <= 0:
                self._send(400, {"error": "empty request"})
                return
            if length > webhook.max_body_bytes:
                self._send(413, {"error": "request too large"})
                return

            raw_body = self.rfile.read(length)
            if webhook.require_signature:
                valid = verify_avigram_signature(
                    raw_body,
                    timestamp=self.headers.get("X-Avigram-Timestamp"),
                    signature=self.headers.get("X-Avigram-Signature"),
                    secret=secret,
                    max_skew_seconds=webhook.max_skew_seconds,
                )
                if not valid:
                    self._send(401, {"error": "invalid signature"})
                    return

            try:
                payload = json.loads(raw_body.decode("utf-8"))
                candidate = candidate_from_avigram_payload(
                    payload,
                    market_name_prefixes=webhook.market_name_prefixes,
                )
                inserted = queue.enqueue(candidate)
                if health_store is not None:
                    health_store.touch_heartbeat(
                        "webhook_last_event",
                        details={
                            "profile": candidate.profile,
                            "id": candidate.avito_id,
                            "queued": inserted,
                        },
                    )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as err:
                logger.warning("Rejected invalid webhook payload: {}", err)
                self._send(400, {"error": "invalid payload"})
                return
            except OSError as err:
                # Returning 5xx lets providers with retry semantics try again;
                # never ACK before SQLite has committed the event.
                logger.error("Could not durably queue webhook event: {}", err)
                self._send(503, {"error": "queue unavailable"})
                return
            except Exception as err:
                logger.exception("Webhook queue failure: {}", err)
                self._send(503, {"error": "queue unavailable"})
                return

            logger.info(
                "webhook accepted source={} profile={} mode={} id={} price={} new={}",
                candidate.source,
                candidate.profile,
                candidate.mode,
                candidate.avito_id,
                candidate.price,
                inserted,
            )
            # Duplicate provider retries are successful deliveries too: the
            # normalized event is already durably present in the queue.
            self._send(202, {"status": "accepted", "queued": inserted})

        def log_message(self, format: str, *args) -> None:
            logger.debug("webhook client={} {}", self.client_address[0], format % args)

    return Handler


def _heartbeat_loop(store: HealthStore, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            store.touch_heartbeat(
                "webhook_ingress",
                details={"pid": os.getpid()},
            )
        except Exception:
            logger.exception("could not write webhook ingress heartbeat")
        stop.wait(30)


def main() -> None:
    config = load_feed_sources_config("deal_sources.toml")
    webhook = config.webhook
    if not webhook.enabled:
        raise SystemExit("webhook.enabled=false in deal_sources.toml")

    deal_config = load_deal_watcher_config("deal_watcher.toml")
    queue = FeedEventQueue(deal_config.database_path)
    health_store = (
        HealthStore(deal_config.database_path)
        if deal_config.health.enabled
        else None
    )
    external_health_timeout = max(
        180,
        deal_config.health.failure_grace_seconds
        + deal_config.health.check_interval_seconds * 3,
    )
    handler = build_handler(
        config,
        queue,
        health_store,
        health_monitor_timeout_seconds=external_health_timeout,
    )
    # Provider retries can arrive in parallel. ThreadingHTTPServer keeps one
    # slow SQLite writer from blocking unrelated health checks/callbacks; SQLite
    # still serializes commits with WAL + busy_timeout underneath.
    server = ThreadingHTTPServer((webhook.bind_host, webhook.port), handler)
    stop_heartbeat = threading.Event()
    heartbeat_thread = None
    if health_store is not None:
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(health_store, stop_heartbeat),
            name="webhook-health-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()

    logger.info(
        "Webhook ingress listening on {}:{}{}; provider={}; queue_db={}",
        webhook.bind_host,
        webhook.port,
        webhook.path,
        webhook.provider,
        deal_config.database_path,
    )
    logger.info(
        "Ingress makes no Avito requests. Put TLS/reverse proxy in front before exposing it publicly."
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("Webhook ingress stopped")
    finally:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    main()
