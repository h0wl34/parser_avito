from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deal_watcher.config import SafetyConfig, load_deal_watcher_config
from deal_watcher.safety import (
    HourlyRequestBudget,
    PersistentBlockCircuit,
    jittered_interval,
    validate_direct_runtime,
    validate_safe_runtime,
)
from deal_watcher.search_profiles import SearchProfile


class DummyConfig:
    use_own_cookies = False
    use_bypass_api = False
    proxy_change_url = ""
    parse_views = False
    parse_phone = False


class SafetyConfigTests(unittest.TestCase):
    def test_load_safe_mode_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deal_watcher.toml"
            path.write_text(
                """
[deal_watcher]
enabled = true

[safety]
enabled = true
require_anonymous = true
stop_on_block = true
allow_direct_avito_requests = false
min_profile_interval_seconds = 600
max_requests_per_hour = 90
interval_jitter_ratio = 0.20
startup_spread_seconds = 120
cooldown_after_block_seconds = 14400
max_cooldown_after_block_seconds = 86400
block_backoff_multiplier = 2.0
block_statuses = [403, 429]
""",
                encoding="utf-8",
            )
            config = load_deal_watcher_config(path)
            self.assertTrue(config.safety.enabled)
            self.assertFalse(config.safety.allow_direct_avito_requests)
            self.assertEqual(config.safety.min_profile_interval_seconds, 600)
            self.assertEqual(config.safety.max_requests_per_hour, 90)
            self.assertEqual(config.safety.block_statuses, (403, 429))

    def test_safe_mode_rejects_account_cookies(self):
        config = DummyConfig()
        config.use_own_cookies = True
        with self.assertRaisesRegex(ValueError, "use_own_cookies"):
            validate_safe_runtime(config, SafetyConfig())

    def test_safe_mode_rejects_enrichment_requests(self):
        config = DummyConfig()
        config.parse_views = True
        with self.assertRaisesRegex(ValueError, "parse_views"):
            validate_safe_runtime(config, SafetyConfig())

    def test_direct_source_is_opt_in(self):
        config = DummyConfig()
        with self.assertRaisesRegex(ValueError, "blocks direct Avito polling"):
            validate_direct_runtime(config, SafetyConfig())
        validate_direct_runtime(
            config,
            SafetyConfig(allow_direct_avito_requests=True),
        )


class SchedulerSafetyTests(unittest.TestCase):
    def test_hourly_budget_is_rolling(self):
        budget = HourlyRequestBudget(limit=3)
        budget.consume(2, now=100.0)
        self.assertEqual(budget.seconds_until_available(1, now=200.0), 0.0)
        budget.consume(1, now=200.0)
        wait = budget.seconds_until_available(1, now=300.0)
        self.assertAlmostEqual(wait, 3400.0)
        self.assertEqual(budget.seconds_until_available(1, now=3701.0), 0.0)

    def test_jitter_respects_floor_and_range(self):
        safety = SafetyConfig(
            min_profile_interval_seconds=300,
            interval_jitter_ratio=0.25,
        )
        profile = SearchProfile(
            name="fast",
            url="https://www.avito.ru/moskva/noutbuki",
            mode="fast",
            interval_seconds=600,
            pages=1,
        )
        with patch("deal_watcher.safety.random.uniform", return_value=500.0):
            self.assertEqual(jittered_interval(profile, safety), 500.0)

        too_fast = SearchProfile(
            name="fast-floor",
            url="https://www.avito.ru/moskva/noutbuki",
            mode="fast",
            interval_seconds=120,
            pages=1,
        )
        with patch("deal_watcher.safety.random.uniform", return_value=250.0):
            self.assertEqual(jittered_interval(too_fast, safety), 300.0)

    def test_persistent_block_circuit_survives_recreation_and_backs_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "state.db")
            safety = SafetyConfig(
                cooldown_after_block_seconds=3600,
                max_cooldown_after_block_seconds=14400,
                block_backoff_multiplier=2.0,
            )
            first = PersistentBlockCircuit(db_path)
            self.assertEqual(first.record_block(safety, now=1000.0), 3600.0)
            self.assertAlmostEqual(first.seconds_remaining(now=1001.0), 3599.0)

            recreated = PersistentBlockCircuit(db_path)
            self.assertAlmostEqual(recreated.seconds_remaining(now=1001.0), 3599.0)
            self.assertEqual(recreated.record_block(safety, now=5000.0), 7200.0)
            self.assertAlmostEqual(recreated.seconds_remaining(now=5001.0), 7199.0)

            recreated.record_success(now=13000.0)
            self.assertEqual(recreated.seconds_remaining(now=13000.0), 0.0)


if __name__ == "__main__":
    unittest.main()
