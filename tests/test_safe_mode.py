from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deal_watcher.config import SafetyConfig, load_deal_watcher_config
from deal_watcher.search_profiles import SearchProfile
from deal_watcher_runner import (
    HourlyRequestBudget,
    _jittered_interval,
    _validate_safe_runtime,
)
from dto import AvitoConfig


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
min_profile_interval_seconds = 600
max_requests_per_hour = 90
interval_jitter_ratio = 0.20
startup_spread_seconds = 120
cooldown_after_block_seconds = 14400
block_statuses = [403, 429]
""",
                encoding="utf-8",
            )
            config = load_deal_watcher_config(path)
            self.assertTrue(config.safety.enabled)
            self.assertEqual(config.safety.min_profile_interval_seconds, 600)
            self.assertEqual(config.safety.max_requests_per_hour, 90)
            self.assertEqual(config.safety.block_statuses, (403, 429))

    def test_safe_mode_rejects_account_cookies(self):
        config = AvitoConfig(urls=[], use_own_cookies=True)
        with self.assertRaisesRegex(ValueError, "use_own_cookies"):
            _validate_safe_runtime(config, SafetyConfig())

    def test_safe_mode_rejects_enrichment_requests(self):
        config = AvitoConfig(urls=[], parse_views=True)
        with self.assertRaisesRegex(ValueError, "parse_views"):
            _validate_safe_runtime(config, SafetyConfig())


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
        with patch("deal_watcher_runner.random.uniform", return_value=500.0):
            self.assertEqual(_jittered_interval(profile, safety), 500.0)

        too_fast = SearchProfile(
            name="fast-floor",
            url="https://www.avito.ru/moskva/noutbuki",
            mode="fast",
            interval_seconds=120,
            pages=1,
        )
        with patch("deal_watcher_runner.random.uniform", return_value=250.0):
            self.assertEqual(_jittered_interval(too_fast, safety), 300.0)


if __name__ == "__main__":
    unittest.main()
