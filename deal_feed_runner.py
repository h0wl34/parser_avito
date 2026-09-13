from __future__ import annotations

from email import policy
from email.parser import BytesParser
import imaplib
import os
from pathlib import Path
import time

from loguru import logger

from deal_watcher import DealWatcherService, load_deal_watcher_config
from deal_watcher.feed import ListingCandidate, candidates_from_email, candidates_from_json_lines
from deal_watcher.feed_config import FeedSourcesConfig, load_feed_sources_config
from deal_watcher.formatter import format_deal_markdown
from integrations.notifications.factory import build_notifier
from load_config import load_avito_config


class FeedProcessor:
    def __init__(self, *, avito_config_path: str, deal_config_path: str):
        self.base_config = load_avito_config(avito_config_path)
        self.deal_config = load_deal_watcher_config(deal_config_path)
        if not self.deal_config.enabled:
            raise ValueError("Laptop Deal Watcher is disabled")
        self.service = DealWatcherService(self.deal_config)
        self.notifier = build_notifier(self.base_config)

    def process(self, candidate: ListingCandidate) -> bool:
        store = self.service.store
        if store.is_seen(candidate.profile, candidate.avito_id, candidate.price):
            logger.debug(
                "feed duplicate profile={} id={} price={}",
                candidate.profile,
                candidate.avito_id,
                candidate.price,
            )
            return False

        item = candidate.to_item()
        analysis = self.service.analyze_item(
            item,
            source_url=f"feed:{candidate.source}:{candidate.profile}",
            baseline_eligible=candidate.baseline_eligible,
        )
        store.mark_seen(candidate.profile, [(candidate.avito_id, candidate.price)])
        if analysis is None:
            logger.warning(
                "feed skipped unscorable listing profile={} id={}",
                candidate.profile,
                candidate.avito_id,
            )
            return False

        item.dealAnalysis = analysis
        logger.info(
            "feed source={} profile={} mode={} score={} label={} price={} title={!r}",
            candidate.source,
            candidate.profile,
            candidate.mode,
            analysis.score,
            analysis.label,
            candidate.price,
            candidate.title,
        )

        if candidate.mode == "fast" and self.service.should_notify(analysis):
            # Feed events intentionally send text only. Fetching an image from
            # Avito would turn a zero-request acquisition path into an extra
            # direct request and defeats the architecture.
            self.notifier.notify(message=format_deal_markdown(item, analysis))
            logger.info(
                "feed notification sent profile={} id={} score={}",
                candidate.profile,
                candidate.avito_id,
                analysis.score,
            )
            return True
        return False


def _ensure_feed_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def _run_jsonl_once(
    *,
    config: FeedSourcesConfig,
    processor: FeedProcessor,
    offsets: dict[str, int],
) -> int:
    source = config.jsonl
    if not source.enabled:
        return 0

    path = Path(source.path)
    _ensure_feed_file(path)
    key = str(path.resolve())
    offset = offsets.get(key)
    if offset is None:
        offset = path.stat().st_size if source.start_at_end else 0

    size = path.stat().st_size
    if size < offset:
        logger.warning("JSONL feed was truncated; restarting at byte 0")
        offset = 0

    processed = 0
    with path.open("r", encoding="utf-8") as fh:
        fh.seek(offset)
        while True:
            line_start = fh.tell()
            line = fh.readline()
            if not line:
                break
            try:
                candidates = list(candidates_from_json_lines([line]))
                for candidate in candidates:
                    processor.process(candidate)
                    processed += 1
            except Exception as err:
                # A bad event must not poison the whole feed. It is consumed and
                # logged with its byte offset, but credentials/content are not.
                logger.error("invalid JSONL feed event at byte {}: {}", line_start, err)
        offsets[key] = fh.tell()
    return processed


def _imap_credentials(config: FeedSourcesConfig) -> tuple[str, str]:
    source = config.imap
    username = os.environ.get(source.username_env, "")
    password = os.environ.get(source.password_env, "")
    if source.enabled and (not username or not password):
        raise ValueError(
            f"IMAP credentials missing: set {source.username_env} and {source.password_env}"
        )
    return username, password


def _run_imap_once(
    *,
    config: FeedSourcesConfig,
    processor: FeedProcessor,
) -> int:
    source = config.imap
    if not source.enabled:
        return 0

    username, password = _imap_credentials(config)
    processed = 0
    with imaplib.IMAP4_SSL(source.host, source.port, timeout=30) as conn:
        conn.login(username, password)
        status, _ = conn.select(source.folder)
        if status != "OK":
            raise RuntimeError(f"cannot select IMAP folder {source.folder!r}")

        if source.sender_contains:
            sender = source.sender_contains.replace('"', "")
            criterion = f'(UNSEEN FROM "{sender}")'
        else:
            criterion = "UNSEEN"
        status, data = conn.uid("search", None, criterion)
        if status != "OK":
            raise RuntimeError("IMAP search failed")

        uids = data[0].split() if data and data[0] else []
        for uid in uids:
            status, rows = conn.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not rows:
                logger.warning("IMAP fetch failed for uid={}", uid.decode(errors="ignore"))
                continue
            raw = next(
                (row[1] for row in rows if isinstance(row, tuple) and len(row) > 1),
                None,
            )
            if not isinstance(raw, (bytes, bytearray)):
                continue

            message = BytesParser(policy=policy.default).parsebytes(bytes(raw))
            candidates = candidates_from_email(
                message,
                profile=source.profile,
                mode=source.mode,
                source="imap",
            )
            if not candidates:
                logger.warning(
                    "IMAP notification uid={} contained no reliably parseable listing; "
                    "send a raw sample to adapt the parser if this persists",
                    uid.decode(errors="ignore"),
                )
            for candidate in candidates:
                processor.process(candidate)
                processed += 1

            if source.mark_seen:
                conn.uid("store", uid, "+FLAGS", "(\\Seen)")
    return processed


def run(
    *,
    avito_config_path: str = "config.toml",
    deal_config_path: str = "deal_watcher.toml",
    sources_config_path: str = "deal_sources.toml",
) -> None:
    sources = load_feed_sources_config(sources_config_path)
    processor = FeedProcessor(
        avito_config_path=avito_config_path,
        deal_config_path=deal_config_path,
    )
    _imap_credentials(sources)

    enabled = []
    if sources.jsonl.enabled:
        enabled.append(f"jsonl:{sources.jsonl.path}")
    if sources.imap.enabled:
        enabled.append(f"imap:{sources.imap.host}/{sources.imap.folder}")
    logger.info(
        "Deal Feed Runner started; sources={}; direct Avito requests=0",
        ", ".join(enabled),
    )

    offsets: dict[str, int] = {}
    last_jsonl = 0.0
    last_imap = 0.0
    while True:
        now = time.monotonic()
        did_work = False
        try:
            if sources.jsonl.enabled and now - last_jsonl >= sources.jsonl.poll_seconds:
                did_work = bool(
                    _run_jsonl_once(config=sources, processor=processor, offsets=offsets)
                ) or did_work
                last_jsonl = now

            if sources.imap.enabled and now - last_imap >= sources.imap.poll_seconds:
                did_work = bool(
                    _run_imap_once(config=sources, processor=processor)
                ) or did_work
                last_imap = now
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.exception("feed source iteration failed; retrying without touching Avito")

        time.sleep(0.25 if did_work else 1.0)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        logger.info("Deal Feed Runner stopped")
