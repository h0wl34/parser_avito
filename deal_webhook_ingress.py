from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from typing import Type

from loguru import logger

from deal_watcher import load_deal_watcher_config
from deal_watcher.feed_config import FeedSourcesConfig, load_feed_sources_config
from deal_watcher.feed_queue import FeedEventQueue
from deal_watcher.webhook import (
    candidate_from_avigram_payload,
    verify_avigram_signature,
)


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_handler(
    config: FeedSourcesConfig,
    queue: FeedEventQueue,
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
        server_version = "DealWatcherIngress/2.0"

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
                self._send(
                    200,
                    {
                        "status": "ok",
                        "ingress": webhook.provider,
                        "queue": queue.stats(),
                    },
                )
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


def main() -> None:
    config = load_feed_sources_config("deal_sources.toml")
    webhook = config.webhook
    if not webhook.enabled:
        raise SystemExit("webhook.enabled=false in deal_sources.toml")

    deal_config = load_deal_watcher_config("deal_watcher.toml")
    queue = FeedEventQueue(deal_config.database_path)
    handler = build_handler(config, queue)
    server = HTTPServer((webhook.bind_host, webhook.port), handler)
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
        server.server_close()


if __name__ == "__main__":
    main()
