from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from deal_watcher.health import HealthStore


UTC = timezone.utc


class HealthIncidentTests(unittest.TestCase):
    def test_transient_failure_recovers_without_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HealthStore(Path(tmp) / "test.db")
            t0 = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
            store.observe(
                "telegram_path",
                healthy=False,
                severity="CRITICAL",
                summary="timeout",
                grace_seconds=120,
                consecutive_required=3,
                repeat_alert_seconds=3600,
                now=t0,
            )
            store.observe(
                "telegram_path",
                healthy=False,
                severity="CRITICAL",
                summary="timeout",
                grace_seconds=120,
                consecutive_required=3,
                repeat_alert_seconds=3600,
                now=t0 + timedelta(seconds=60),
            )
            state = store.observe(
                "telegram_path",
                healthy=True,
                severity="CRITICAL",
                summary="healthy",
                grace_seconds=120,
                consecutive_required=3,
                repeat_alert_seconds=3600,
                now=t0 + timedelta(seconds=90),
            )
            self.assertEqual(state, "healthy")
            self.assertIsNone(store.claim_alert(now=t0 + timedelta(minutes=10)))
            self.assertEqual(store.active_incidents(), [])

    def test_persistent_failure_opens_and_recovery_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HealthStore(Path(tmp) / "test.db")
            t0 = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
            for seconds in (0, 60):
                store.observe(
                    "feed_worker",
                    healthy=False,
                    severity="CRITICAL",
                    summary="heartbeat stale",
                    grace_seconds=120,
                    consecutive_required=3,
                    repeat_alert_seconds=3600,
                    now=t0 + timedelta(seconds=seconds),
                )
            state = store.observe(
                "feed_worker",
                healthy=False,
                severity="CRITICAL",
                summary="heartbeat stale",
                grace_seconds=120,
                consecutive_required=3,
                repeat_alert_seconds=3600,
                now=t0 + timedelta(seconds=120),
            )
            self.assertEqual(state, "opened")
            alert = store.claim_alert(now=t0 + timedelta(seconds=120))
            self.assertIsNotNone(alert)
            self.assertEqual(alert.kind, "open")
            store.alert_delivered(alert.alert_id, now=t0 + timedelta(seconds=121))

            state = store.observe(
                "feed_worker",
                healthy=True,
                severity="CRITICAL",
                summary="heartbeat healthy",
                grace_seconds=120,
                consecutive_required=3,
                repeat_alert_seconds=3600,
                now=t0 + timedelta(seconds=180),
            )
            self.assertEqual(state, "recovered")
            recovery = store.claim_alert(now=t0 + timedelta(seconds=180))
            self.assertIsNotNone(recovery)
            self.assertEqual(recovery.kind, "recovery")
            self.assertIn("RECOVERED", recovery.message)
            self.assertEqual(store.active_incidents(), [])

    def test_repeat_waits_for_repeat_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HealthStore(Path(tmp) / "test.db")
            t0 = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
            store.observe(
                "dead_letter",
                healthy=False,
                severity="CRITICAL",
                summary="dead event",
                grace_seconds=0,
                consecutive_required=1,
                repeat_alert_seconds=3600,
                now=t0,
            )
            first = store.claim_alert(now=t0)
            store.alert_delivered(first.alert_id, now=t0)
            state = store.observe(
                "dead_letter",
                healthy=False,
                severity="CRITICAL",
                summary="dead event",
                grace_seconds=0,
                consecutive_required=1,
                repeat_alert_seconds=3600,
                now=t0 + timedelta(seconds=3599),
            )
            self.assertEqual(state, "active")
            self.assertIsNone(store.claim_alert(now=t0 + timedelta(seconds=3599)))
            state = store.observe(
                "dead_letter",
                healthy=False,
                severity="CRITICAL",
                summary="dead event",
                grace_seconds=0,
                consecutive_required=1,
                repeat_alert_seconds=3600,
                now=t0 + timedelta(seconds=3601),
            )
            self.assertEqual(state, "repeated")
            self.assertEqual(store.claim_alert(now=t0 + timedelta(seconds=3601)).kind, "repeat")

    def test_heartbeat_survives_recreation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            at = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
            HealthStore(db).touch_heartbeat("feed_worker", details="pid=42", now=at)
            heartbeat = HealthStore(db).heartbeat("feed_worker")
            self.assertEqual(heartbeat.updated_at, at)
            self.assertEqual(heartbeat.details, "pid=42")


if __name__ == "__main__":
    unittest.main()
