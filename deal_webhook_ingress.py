from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from typing import Type

from loguru import logger

from deal_watcher.feed_config import FeedSourcesConfig, load_feed_sources_config
from deal_watcher.webhook import (
    append_candidate_durable,
    candidate_from_avigram_payload,
    verify_avigram_signature,
)


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_handler(config: FeedSourcesConfig) -> Type[BaseHTTPRequestHandler]:
    webhook = config.webhook
    spool_path = config.jsonl.path
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
        server_version = "DealWatcherIngress/1.0"

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
                self._send(200, {"status": "ok", "ingress": webhook.provider})
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
                append_candidate_durable(candidate, spool_path)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as err:
                logger.warning("Rejected invalid webhook payload: {}", err)
                self._send(400, {"error": "invalid payload"})
                return
            except OSError as err:
                # Returning 5xx lets providers with retry semantics try again;
                # never ACK before the event is durably appended.
                logger.error("Could not durably spool webhook event: {}", err)
                self._send(503, {"error": "spool unavailable"})
                return

            logger.info(
                "webhook accepted source={} profile={} mode={} id={} price={}",
                candidate.source,
                candidate.profile,
                candidate.mode,
                candidate.avito_id,
                candidate.price,
            )
            self._send(202, {"status": "accepted"})

        def log_message(self, format: str, *args) -> None:
            logger.debug("webhook client={} {}", self.client_address[0], format % args)

    return Handler


def main() -> None:
    config = load_feed_sources_config("deal_sources.toml")
    webhook = config.webhook
    if not webhook.enabled:
        raise SystemExit("webhook.enabled=false in deal_sources.toml")

    handler = build_handler(config)
    server = HTTPServer((webhook.bind_host, webhook.port), handler)
    logger.info(
        "Webhook ingress listening on {}:{}{}; provider={}; spool={}",
        webhook.bind_host,
        webhook.port,
        webhook.path,
        webhook.provider,
        config.jsonl.path,
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
