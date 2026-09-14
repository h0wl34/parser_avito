from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from deal_health_monitor import _drain_system_alerts, _heartbeat_observation
from deal_watcher.feed import ListingCandidate
from deal_watcher.feed_queue import FeedEventQueue
from deal_watcher.health import HealthStore, queue_health_snapshot


UTC = timezone.utc


class FakeSender:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, message: str):
        self.messages.append(message)
        return True, "fake"


class HealthRuntimeTests(unittest.TestCase):
    def test_queue_snapshot_reports_dead_and_old_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            queue = FeedEventQueue(db)
            t0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
            first = ListingCandidate(
                avito_id=1,
                title="Lenovo Legion 5 RTX 4070",
                price=99_000,
                url="https://www.avito.ru/x_1",
                profile="fast-any-4070",
                mode="fast",
                source="test",
            )
            queue.enqueue(first, observed_at=t0)
            event = queue.claim_due(now=t0)
            queue.dead_letter(event.event_key, error="poison", now=t0)

            second = ListingCandidate(
                avito_id=2,
                title="ASUS TUF RTX 4070",
                price=90_000,
                url="https://www.avito.ru/x_2",
                profile="fast-any-4070",
                mode="fast",
                source="test",
            )
            queue.enqueue(second, observed_at=t0 + timedelta(seconds=20))
            snapshot = queue_health_snapshot(db, now=t0 + timedelta(seconds=620))
            self.assertEqual(snapshot.dead, 1)
            self.assertEqual(snapshot.pending, 1)
            self.assertAlmostEqual(snapshot.oldest_unfinished_age_seconds, 600, places=0)

    def test_process_heartbeat_timeout_is_not_double_graced(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HealthStore(Path(tmp) / "test.db")
            t0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
            store.touch_heartbeat("feed_worker", now=t0)

            # Heartbeat itself remains healthy through the configured timeout.
            healthy, _ = _heartbeat_observation(
                store,
                service="feed_worker",
                timeout_seconds=90,
                now=t0 + timedelta(seconds=90),
            )
            self.assertTrue(healthy)

            # Once stale, three consecutive 30-second observations are enough.
            # There must not be another +120s generic grace period here.
            for seconds in (91, 121):
                healthy, summary = _heartbeat_observation(
                    store,
                    service="feed_worker",
                    timeout_seconds=90,
                    now=t0 + timedelta(seconds=seconds),
                )
                self.assertFalse(healthy)
                state = store.observe(
                    "feed_worker",
                    healthy=healthy,
                    severity="CRITICAL",
                    summary=summary,
                    grace_seconds=0,
                    consecutive_required=3,
                    repeat_alert_seconds=3600,
                    now=t0 + timedelta(seconds=seconds),
                )
                self.assertEqual(state, "debouncing")

            healthy, summary = _heartbeat_observation(
                store,
                service="feed_worker",
                timeout_seconds=90,
                now=t0 + timedelta(seconds=151),
            )
            state = store.observe(
                "feed_worker",
                healthy=healthy,
                severity="CRITICAL",
                summary=summary,
                grace_seconds=0,
                consecutive_required=3,
                repeat_alert_seconds=3600,
                now=t0 + timedelta(seconds=151),
            )
            self.assertEqual(state, "opened")
            alert = store.claim_alert(now=t0 + timedelta(seconds=151))
            self.assertIsNotNone(alert)
            self.assertEqual(alert.incident_key, "feed_worker")

    def test_stale_open_alarm_is_not_sent_after_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HealthStore(Path(tmp) / "test.db")
            sender = FakeSender()
            # Keep synthetic outbox timestamps safely in the past relative to
            # the CI wall clock. claim_alert() intentionally refuses future
            # next_attempt_at values.
            t0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
            store.observe(
                "telegram_path",
                healthy=False,
                severity="CRITICAL",
                summary="path down",
                grace_seconds=0,
                consecutive_required=1,
                repeat_alert_seconds=3600,
                now=t0,
            )
            store.observe(
                "telegram_path",
                healthy=True,
                severity="CRITICAL",
                summary="path healthy",
                grace_seconds=0,
                consecutive_required=1,
                repeat_alert_seconds=3600,
                now=t0 + timedelta(seconds=30),
            )

            delivered = _drain_system_alerts(store=store, sender=sender)
            self.assertEqual(delivered, 1)
            self.assertEqual(len(sender.messages), 1)
            self.assertIn("RECOVERED", sender.messages[0])


if __name__ == "__main__":
    unittest.main()
