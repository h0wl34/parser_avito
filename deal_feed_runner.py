from __future__ import annotations

from datetime import datetime, timezone
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
from deal_watcher.feed_queue import FeedEventQueue
from deal_watcher.formatter import format_deal_markdown
from deal_watcher.health import HealthStore
from deal_watcher.jsonl_queue import file_identity, iter_complete_records
from integrations.notifications.factory import build_notifier
from load_config import load_avito_config


class NotificationDeliveryError(RuntimeError):
    """A high-value feed event could not be delivered to any notifier."""


class FeedProcessor:
    def __init__(self, *, avito_config_path: str, deal_config_path: str):
        self.base_config = load_avito_config(avito_config_path)
        self.deal_config = load_deal_watcher_config(deal_config_path)
        if not self.deal_config.enabled:
            raise ValueError("Laptop Deal Watcher is disabled")
        self.service = DealWatcherService(self.deal_config)
        self.notifier = build_notifier(self.base_config)

    def process(
        self,
        candidate: ListingCandidate,
        *,
        allow_notify: bool = True,
    ) -> bool:
        store = self.service.store
        # On the feed path deal_seen means "an alert for this profile/id/price
        # was successfully delivered", not merely "some revision was analyzed".
        # The durable queue deduplicates identical revisions; edited content at
        # the same price is intentionally re-scored until an alert is delivered.
        if store.is_seen(candidate.profile, candidate.avito_id, candidate.price):
            logger.debug(
                "feed already-alerted profile={} id={} price={}",
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
        if analysis is None:
            logger.warning(
                "feed skipped unscorable listing profile={} id={}; future revisions remain eligible",
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

        should_notify = (
            allow_notify
            and candidate.mode == "fast"
            and self.service.should_notify(analysis)
        )
        if should_notify:
            # Feed events intentionally send text only. Fetching an image from
            # Avito would turn a zero-request acquisition path into an extra
            # direct request and defeats the architecture.
            delivered = self.notifier.notify(
                message=format_deal_markdown(item, analysis)
            )
            if delivered is False:
                raise NotificationDeliveryError(
                    f"all notification backends failed for listing {candidate.avito_id}"
                )

            # There is an unavoidable tiny at-least-once window between the
            # remote Telegram ACK and this local mark. A crash in exactly that
            # interval can duplicate an alert, but it cannot silently lose one.
            store.mark_seen(candidate.profile, [(candidate.avito_id, candidate.price)])
            logger.info(
                "feed notification sent profile={} id={} score={}",
                candidate.profile,
                candidate.avito_id,
                analysis.score,
            )
            return True

        if (
            not allow_notify
            and candidate.mode == "fast"
            and self.service.should_notify(analysis)
        ):
            logger.warning(
                "stale FAST alert suppressed profile={} id={} score={}",
                candidate.profile,
                candidate.avito_id,
                analysis.score,
            )

        # Do not mark low-scoring/MARKET/stale events as alerted. Their queue
        # event is ACKed by the worker, while a later fresh revision can still
        # be scored and delivered.
        return False


def _ensure_feed_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def _ingest_jsonl_once(
    *,
    config: FeedSourcesConfig,
    processor: FeedProcessor,
    queue: FeedEventQueue,
) -> int:
    """Move complete JSONL records into the durable SQLite event queue."""
    source = config.jsonl
    if not source.enabled:
        return 0

    path = Path(source.path)
    _ensure_feed_file(path)
    resolved = str(path.resolve())
    source_key = f"jsonl:{resolved}"
    identity = file_identity(path)
    size = path.stat().st_size

    store = processor.service.store
    checkpoint = store.get_feed_cursor(source_key)
    if checkpoint is None:
        offset = size if source.start_at_end else 0
        store.save_feed_cursor(source_key, identity, offset)
    else:
        checkpoint_identity, offset = checkpoint
        if checkpoint_identity != identity:
            logger.warning(
                "JSONL feed file was replaced/rotated; restarting new file at byte 0"
            )
            offset = 0
            store.save_feed_cursor(source_key, identity, offset)
        elif size < offset:
            logger.warning("JSONL feed was truncated; restarting at byte 0")
            offset = 0
            store.save_feed_cursor(source_key, identity, offset)

    ingested = 0
    for record in iter_complete_records(path, offset=offset):
        try:
            candidates = list(candidates_from_json_lines([record.line]))
        except Exception as err:
            # A complete but malformed record is poison input. Incomplete final
            # records are not yielded by iter_complete_records at all.
            logger.error(
                "invalid JSONL feed event at byte {}: {}",
                record.start_offset,
                err,
            )
            store.save_feed_cursor(source_key, identity, record.end_offset)
            continue

        try:
            for candidate in candidates:
                queue.enqueue(candidate)
                ingested += 1
        except Exception as err:
            # Queue commit failed: do not move the source cursor. The record is
            # retried after restart/next cycle.
            logger.error(
                "could not durably enqueue JSONL event at byte {}; will retry: {}",
                record.start_offset,
                err,
            )
            return ingested

        store.save_feed_cursor(source_key, identity, record.end_offset)
    return ingested


def _imap_credentials(config: FeedSourcesConfig) -> tuple[str, str]:
    source = config.imap
    username = os.environ.get(source.username_env, "")
    password = os.environ.get(source.password_env, "")
    if source.enabled and (not username or not password):
        raise ValueError(
            f"IMAP credentials missing: set {source.username_env} and {source.password_env}"
        )
    return username, password


def _ingest_imap_once(
    *,
    config: FeedSourcesConfig,
    queue: FeedEventQueue,
) -> int:
    """Move parseable notification emails into the durable SQLite queue."""
    source = config.imap
    if not source.enabled:
        return 0

    username, password = _imap_credentials(config)
    ingested = 0
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
                logger.error(
                    "IMAP notification uid={} contained no reliably parseable listing; "
                    "leaving it unread for investigation",
                    uid.decode(errors="ignore"),
                )
                continue

            try:
                for candidate in candidates:
                    queue.enqueue(candidate)
                    ingested += 1
            except Exception:
                # No email ACK unless every candidate is durably in SQLite.
                logger.exception(
                    "could not durably enqueue IMAP uid={}; leaving unread",
                    uid.decode(errors="ignore"),
                )
                continue

            if source.mark_seen:
                conn.uid("store", uid, "+FLAGS", "(\\Seen)")
    return ingested


def _retry_delay(attempts: int) -> int:
    # Fast first retries matter for a bargain alert; prolonged outages back off
    # to five minutes so Telegram failures cannot spin the worker.
    schedule = (5, 15, 30, 60, 120, 300)
    return schedule[min(max(1, attempts), len(schedule)) - 1]


def _drain_queue_once(
    *,
    processor: FeedProcessor,
    queue: FeedEventQueue,
    max_events: int = 50,
) -> int:
    processed = 0
    for _ in range(max(1, max_events)):
        event = queue.claim_due(lease_seconds=120)
        if event is None:
            break

        now = datetime.now(timezone.utc)
        max_age = processor.deal_config.max_fast_event_age_seconds
        queue_age = max(0.0, (now - event.created_at).total_seconds())
        allow_notify = not (
            event.candidate.mode == "fast"
            and max_age > 0
            and queue_age > max_age
        )
        if not allow_notify:
            logger.warning(
                "FAST event too old for alert delivery event={} profile={} queue_age={:.0f}s max={}s",
                event.event_key[:12],
                event.candidate.profile,
                queue_age,
                max_age,
            )

        try:
            processor.process(event.candidate, allow_notify=allow_notify)
        except NotificationDeliveryError as err:
            delay = _retry_delay(event.attempts)
            queue.retry(
                event.event_key,
                error=str(err),
                delay_seconds=delay,
            )
            logger.warning(
                "notification delivery failed event={} attempt={}; retry in {}s",
                event.event_key[:12],
                event.attempts,
                delay,
            )
        except Exception as err:
            # Valid normalized events should rarely fail deterministically. Give
            # them several retries, then isolate poison data instead of letting
            # one event churn forever.
            if event.attempts >= 10:
                queue.dead_letter(event.event_key, error=str(err))
                logger.exception(
                    "feed event={} moved to dead-letter after {} attempts",
                    event.event_key[:12],
                    event.attempts,
                )
            else:
                delay = min(900, 15 * (2 ** min(event.attempts - 1, 6)))
                queue.retry(
                    event.event_key,
                    error=str(err),
                    delay_seconds=delay,
                )
                logger.exception(
                    "feed event={} attempt={} failed; retry in {}s",
                    event.event_key[:12],
                    event.attempts,
                    delay,
                )
        else:
            queue.ack(event.event_key)
            processed += 1
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
    queue = FeedEventQueue(processor.deal_config.database_path)
    health_store = (
        HealthStore(processor.deal_config.database_path)
        if processor.deal_config.health.enabled
        else None
    )
    _imap_credentials(sources)

    enabled = []
    if sources.jsonl.enabled:
        enabled.append(f"jsonl:{sources.jsonl.path}")
    if sources.imap.enabled:
        enabled.append(f"imap:{sources.imap.host}/{sources.imap.folder}")
    if sources.webhook.enabled:
        enabled.append(f"webhook-queue:{sources.webhook.provider}")
    logger.info(
        "Deal Feed Runner started; sources={}; direct Avito requests=0",
        ", ".join(enabled),
    )

    last_jsonl = 0.0
    last_imap = 0.0
    last_health = 0.0
    last_prune = 0.0
    last_heartbeat = 0.0
    while True:
        now = time.monotonic()
        did_work = False
        try:
            if health_store is not None and now - last_heartbeat >= 30:
                health_store.touch_heartbeat(
                    "feed_worker",
                    details={"pid": os.getpid()},
                )
                last_heartbeat = now

            if sources.jsonl.enabled and now - last_jsonl >= sources.jsonl.poll_seconds:
                did_work = bool(
                    _ingest_jsonl_once(
                        config=sources,
                        processor=processor,
                        queue=queue,
                    )
                ) or did_work
                last_jsonl = now

            if sources.imap.enabled and now - last_imap >= sources.imap.poll_seconds:
                did_work = bool(
                    _ingest_imap_once(config=sources, queue=queue)
                ) or did_work
                last_imap = now

            did_work = bool(
                _drain_queue_once(processor=processor, queue=queue)
            ) or did_work

            if now - last_health >= 60:
                logger.info("feed queue stats={}", queue.stats())
                last_health = now

            if now - last_prune >= 86_400:
                removed = queue.prune_done(keep_days=14)
                if removed:
                    logger.info("pruned {} old completed feed events", removed)
                last_prune = now
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.exception("feed iteration failed; retrying without touching Avito")

        time.sleep(0.25 if did_work else 1.0)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        logger.info("Deal Feed Runner stopped")
