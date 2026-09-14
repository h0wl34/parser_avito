from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
from typing import Type

from loguru import logger

from deal_watcher import load_deal_watcher_config
from deal_watcher.health import HealthStore
from deal_watcher.phone import PhoneNotificationStore, verify_phone_signature


DEFAULT_MAX_BODY_BYTES = 524_288
DEFAULT_MAX_SKEW_SECONDS = 300


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_handler(
    store: PhoneNotificationStore,
    *,
    secret: str,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
) -> Type[BaseHTTPRequestHandler]:
    if not secret:
        raise ValueError("phone ingress secret must not be empty")

    class Handler(BaseHTTPRequestHandler):
        server_version = "DealPhoneIngress/1.0"

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
                self._send(200, {"status": "ok", "stored": store.count()})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/phone-notification":
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
            if length > max_body_bytes:
                self._send(413, {"error": "request too large"})
                return

            raw_body = self.rfile.read(length)
            if not verify_phone_signature(
                raw_body,
                timestamp=self.headers.get("X-Phone-Timestamp"),
                signature=self.headers.get("X-Phone-Signature"),
                secret=secret,
                max_skew_seconds=max_skew_seconds,
            ):
                self._send(401, {"error": "invalid signature"})
                return

            try:
                payload = json.loads(raw_body.decode("utf-8"))
                inserted = store.store(payload, raw_body=raw_body)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as err:
                logger.warning("Rejected invalid phone payload: {}", err)
                self._send(400, {"error": "invalid payload"})
                return
            except OSError as err:
                logger.error("Could not durably store phone notification: {}", err)
                self._send(503, {"error": "storage unavailable"})
                return
            except Exception as err:
                logger.exception("Phone ingress storage failure: {}", err)
                self._send(503, {"error": "storage unavailable"})
                return

            logger.info(
                "phone notification accepted package={} event={} new={}",
                payload.get("package"),
                str(payload.get("event_id", ""))[:12],
                inserted,
            )
            self._send(202, {"status": "accepted", "stored": inserted})

        def log_message(self, format: str, *args) -> None:
            logger.debug("phone ingress client={} {}", self.client_address[0], format % args)

    return Handler


def _heartbeat_loop(store: HealthStore, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            store.touch_heartbeat("phone_ingress", details={"pid": os.getpid()})
        except Exception:
            logger.exception("could not write phone ingress heartbeat")
        stop.wait(30)


def main() -> None:
    secret = os.environ.get("DEAL_PHONE_SECRET", "").strip()
    if not secret:
        raise SystemExit("DEAL_PHONE_SECRET is required")

    bind_host = os.environ.get("DEAL_PHONE_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("DEAL_PHONE_PORT", "8767"))
    max_body_bytes = int(os.environ.get("DEAL_PHONE_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES)))
    max_skew_seconds = int(os.environ.get("DEAL_PHONE_MAX_SKEW_SECONDS", str(DEFAULT_MAX_SKEW_SECONDS)))

    deal_config = load_deal_watcher_config("deal_watcher.toml")
    store = PhoneNotificationStore(deal_config.database_path)
    health_store = HealthStore(deal_config.database_path) if deal_config.health.enabled else None
    handler = build_handler(
        store,
        secret=secret,
        max_body_bytes=max_body_bytes,
        max_skew_seconds=max_skew_seconds,
    )
    server = ThreadingHTTPServer((bind_host, port), handler)

    stop_heartbeat = threading.Event()
    heartbeat_thread = None
    if health_store is not None:
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(health_store, stop_heartbeat),
            name="phone-ingress-health-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()

    logger.info(
        "Phone ingress listening on {}:{}/phone-notification; queue_db={}; Avito requests=0",
        bind_host,
        port,
        deal_config.database_path,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("Phone ingress stopped")
    finally:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    main()
