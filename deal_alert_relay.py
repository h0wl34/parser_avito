from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import time

import requests
from loguru import logger

from deal_watcher.health import verify_relay_signature


MAX_BODY_BYTES = 16 * 1024


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _chat_ids() -> list[str]:
    raw = os.environ.get("DEAL_RELAY_CHAT_ID", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_handler(*, secret: str, bot_token: str, chat_ids: list[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DealAlertRelay/1.0"

        def _send_json(self, status: int, payload: dict) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send_json(200, {"status": "ok"})
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/alert":
                self._send_json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(400, {"error": "invalid content length"})
                return
            if length <= 0:
                self._send_json(400, {"error": "empty request"})
                return
            if length > MAX_BODY_BYTES:
                self._send_json(413, {"error": "request too large"})
                return

            raw = self.rfile.read(length)
            if not verify_relay_signature(
                raw,
                timestamp=self.headers.get("X-Deal-Timestamp"),
                signature=self.headers.get("X-Deal-Signature"),
                secret=secret,
                max_skew_seconds=300,
            ):
                self._send_json(401, {"error": "invalid signature"})
                return

            try:
                payload = json.loads(raw.decode("utf-8"))
                text = str(payload.get("text", "")).strip()
            except (ValueError, TypeError, json.JSONDecodeError):
                self._send_json(400, {"error": "invalid payload"})
                return
            if not text or len(text) > 10_000:
                self._send_json(400, {"error": "invalid text"})
                return

            delivered = 0
            statuses: list[int] = []
            for chat_id in chat_ids:
                try:
                    response = requests.post(
                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                        json={"chat_id": chat_id, "text": text},
                        timeout=10,
                    )
                    statuses.append(response.status_code)
                    if response.status_code == 200:
                        delivered += 1
                except requests.RequestException as err:
                    logger.warning("relay Telegram delivery failed: {}", type(err).__name__)

            if delivered:
                logger.info("emergency alert delivered to {} chat(s)", delivered)
                self._send_json(202, {"status": "accepted", "delivered": delivered})
                return

            logger.error("emergency relay could not deliver alert; statuses={}", statuses)
            self._send_json(503, {"error": "telegram unavailable"})

        def log_message(self, format: str, *args) -> None:
            logger.debug("relay client={} {}", self.client_address[0], format % args)

    return Handler


def main() -> None:
    secret = os.environ.get("DEAL_RELAY_SECRET", "").strip()
    bot_token = os.environ.get("DEAL_RELAY_BOT_TOKEN", "").strip()
    chat_ids = _chat_ids()
    if not secret:
        raise SystemExit("DEAL_RELAY_SECRET is required")
    if not bot_token:
        raise SystemExit("DEAL_RELAY_BOT_TOKEN is required")
    if not chat_ids:
        raise SystemExit("DEAL_RELAY_CHAT_ID is required")

    bind_host = os.environ.get("DEAL_RELAY_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("DEAL_RELAY_PORT", "8770"))
    if not 1 <= port <= 65535:
        raise SystemExit("DEAL_RELAY_PORT must be between 1 and 65535")

    server = ThreadingHTTPServer(
        (bind_host, port),
        build_handler(secret=secret, bot_token=bot_token, chat_ids=chat_ids),
    )
    logger.info(
        "Emergency alert relay listening on {}:{}; direct Telegram egress enabled",
        bind_host,
        port,
    )
    logger.info(
        "Restrict this port to the watcher server IP with the VPS/provider firewall."
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("Emergency alert relay stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
