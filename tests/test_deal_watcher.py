from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from deal_watcher.config import DealWatcherConfig
from deal_watcher.models import Condition, LaptopSpecs, MarketStats
from deal_watcher.normalizer import assess_risk, extract_specs
from deal_watcher.scoring import analyze_deal
from deal_watcher.storage import DealWatcherStore
from deal_watcher.search_profiles import load_search_profiles


class NormalizerTests(unittest.TestCase):
    def test_tuf_listing(self):
        specs = extract_specs(
            "ASUS TUF Gaming A15 FA507XI Ryzen 9 7940HS RTX 4070 32/1TB Новый, не активирован"
        )
        self.assertEqual(specs.brand, "ASUS")
        self.assertEqual(specs.family, "TUF")
        self.assertEqual(specs.sku, "FA507XI")
        self.assertEqual(specs.gpu, "RTX 4070")
        self.assertEqual(specs.cpu, "RYZEN 9 7940HS")
        self.assertEqual(specs.ram_gb, 32)
        self.assertEqual(specs.storage_gb, 1024)
        self.assertEqual(specs.condition, Condition.NEW_CONFIRMED)

    def test_thinkbook_listing(self):
        specs = extract_specs(
            "Lenovo ThinkBook 16+ 2025 Ultra 7 255H RTX5060 32GB 1TB новый"
        )
        self.assertEqual(specs.family, "ThinkBook 16+")
        self.assertEqual(specs.gpu, "RTX 5060")
        self.assertEqual(specs.ram_gb, 32)
        self.assertEqual(specs.storage_gb, 1024)
        self.assertEqual(specs.condition, Condition.NEW_LIKELY)

    def test_bait_and_refurbished(self):
        text = "RTX 4070 ноутбук 69990 цена при оформлении кредита, после ремонта"
        risk = assess_risk(text)
        specs = extract_specs(text)
        self.assertGreaterEqual(risk.score, 40)
        self.assertIn("цена при кредите", risk.flags)
        self.assertEqual(specs.condition, Condition.REFURBISHED)

    def test_like_new_beats_used_substring(self):
        specs = extract_specs(
            "Lenovo Legion RTX 4070. Куплен неделю назад, не пользовался, не подошёл."
        )
        self.assertEqual(specs.condition, Condition.LIKE_NEW)

    def test_ideal_condition_alone_does_not_mean_like_new(self):
        specs = extract_specs(
            "ASUS RTX 4070 в идеальном состоянии, пользовался два года аккуратно"
        )
        self.assertEqual(specs.condition, Condition.USED)

    def test_incomplete_barebone_is_high_risk(self):
        risk = assess_risk(
            "HP Omen RTX 5060. Цена за ноутбук без ОЗУ и SSD, память ставим отдельно."
        )
        self.assertGreaterEqual(risk.score, 50)
        self.assertIn("без ОЗУ/SSD", risk.flags)

    def test_catalog_listing_is_high_risk(self):
        risk = assess_risk(
            "Ноутбуки с гарантией в ассортименте, в наличии ноутбуки разных моделей, цены от 25000"
        )
        self.assertGreaterEqual(risk.score, 30)
        self.assertIn("витрина/ассортимент", risk.flags)


class ScoringTests(unittest.TestCase):
    def test_anomalous_tuf_scores_high(self):
        config = DealWatcherConfig()
        specs = LaptopSpecs(
            brand="ASUS",
            family="TUF",
            gpu="RTX 4070",
            ram_gb=32,
            storage_gb=1024,
            condition=Condition.NEW_CONFIRMED,
        )
        market = MarketStats(
            sample_size=20,
            median_price=125_000,
            p25_price=115_000,
            p10_price=105_000,
            min_price=99_000,
            comparison_level="family_gpu",
        )
        analysis = analyze_deal(
            price=92_000,
            specs=specs,
            risk=assess_risk("новый не активирован"),
            market=market,
            config=config,
        )
        self.assertGreaterEqual(analysis.score, 90)
        self.assertEqual(analysis.label, "IMMEDIATE")

    def test_like_new_gets_positive_condition_credit(self):
        config = DealWatcherConfig()
        specs = LaptopSpecs(
            brand="Lenovo",
            family="Legion 5",
            gpu="RTX 4070",
            ram_gb=32,
            storage_gb=1024,
            condition=Condition.LIKE_NEW,
        )
        analysis = analyze_deal(
            price=95_000,
            specs=specs,
            risk=assess_risk("не пользовался, куплен неделю назад"),
            market=MarketStats(median_price=125_000, sample_size=10, comparison_level="family_gpu"),
            config=config,
        )
        self.assertGreaterEqual(analysis.score, 80)
        self.assertIn("почти новый: минимальное использование", analysis.reasons)

    def test_cold_start_legion_at_4070_threshold_is_strong(self):
        config = DealWatcherConfig()
        specs = LaptopSpecs(
            brand="Lenovo",
            family="Legion 5",
            gpu="RTX 4070",
            ram_gb=32,
            storage_gb=1024,
            condition=Condition.UNKNOWN,
        )
        analysis = analyze_deal(
            price=99_000,
            specs=specs,
            risk=assess_risk(""),
            market=MarketStats(),
            config=config,
        )
        self.assertGreaterEqual(analysis.score, 80)
        self.assertEqual(analysis.label, "STRONG")
        self.assertIn("cold-start: рыночной выборки пока недостаточно", analysis.reasons)

    def test_cold_start_tuf_4070_at_90k_is_immediate(self):
        config = DealWatcherConfig()
        specs = LaptopSpecs(
            brand="ASUS",
            family="TUF",
            gpu="RTX 4070",
            ram_gb=32,
            storage_gb=1024,
            condition=Condition.NEW_CONFIRMED,
        )
        analysis = analyze_deal(
            price=90_000,
            specs=specs,
            risk=assess_risk("новый не активирован"),
            market=MarketStats(),
            config=config,
        )
        self.assertGreaterEqual(analysis.score, 90)
        self.assertEqual(analysis.label, "IMMEDIATE")

    def test_bad_series_and_risk_are_penalized(self):
        config = DealWatcherConfig()
        specs = LaptopSpecs(
            brand="MSI",
            family="MSI Thin",
            gpu="RTX 4070",
            condition=Condition.REFURBISHED,
        )
        analysis = analyze_deal(
            price=92_000,
            specs=specs,
            risk=assess_risk("после ремонта, цена при оформлении кредита"),
            market=MarketStats(),
            config=config,
        )
        self.assertLess(analysis.score, 50)


class SearchProfileTests(unittest.TestCase):
    def test_load_fast_and_market_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deal_searches.toml"
            path.write_text(
                """
[[search]]
name = "market-4070"
url = "https://www.avito.ru/moskva/noutbuki?q=rtx+4070"
mode = "market"
interval_seconds = 21600
pages = 5

[[search]]
name = "fast-bagration"
url = "https://www.avito.ru/moskva/noutbuki?q=rtx+4070"
mode = "fast"
interval_seconds = 180
pages = 1
""",
                encoding="utf-8",
            )
            profiles = load_search_profiles(path)
            self.assertEqual(
                [p.name for p in profiles],
                ["market-4070", "fast-bagration"],
            )
            self.assertFalse(profiles[0].notify)
            self.assertTrue(profiles[1].notify)

    def test_duplicate_profile_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deal_searches.toml"
            path.write_text(
                """
[[search]]
name = "same"
url = "https://www.avito.ru/moskva/noutbuki"
[[search]]
name = "same"
url = "https://www.avito.ru/moskva/noutbuki"
""",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_search_profiles(path)


class StorageTests(unittest.TestCase):
    def test_market_fallback_and_price_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            store = DealWatcherStore(db_path)
            specs = LaptopSpecs(brand="ASUS", family="TUF", gpu="RTX 4070")
            for idx, price in enumerate(
                [120_000, 122_000, 124_000, 126_000, 128_000], start=1
            ):
                store.record_listing(
                    avito_id=idx,
                    title=f"TUF {idx}",
                    seller_id=None,
                    url=None,
                    price=price,
                    specs=specs,
                )

            stats = store.market_stats(specs, min_samples=5)
            self.assertEqual(stats.comparison_level, "family_gpu")
            self.assertEqual(stats.median_price, 124_000)

            store.record_listing(
                avito_id=99,
                title="TUF deal",
                seller_id=None,
                url=None,
                price=110_000,
                specs=specs,
            )
            drop = store.record_listing(
                avito_id=99,
                title="TUF deal",
                seller_id=None,
                url=None,
                price=99_000,
                specs=specs,
            )
            self.assertAlmostEqual(drop, 10.0, places=1)
            stats_after_drop = store.market_stats(specs, min_samples=5)
            self.assertEqual(stats_after_drop.sample_size, 6)
            self.assertEqual(stats_after_drop.median_price, 123_000)

    def test_market_excludes_high_risk_used_and_like_new_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DealWatcherStore(Path(tmp) / "test.db")
            clean = LaptopSpecs(
                brand="ASUS",
                family="TUF",
                gpu="RTX 4070",
                condition=Condition.NEW_LIKELY,
            )
            used = LaptopSpecs(
                brand="ASUS",
                family="TUF",
                gpu="RTX 4070",
                condition=Condition.USED,
            )
            like_new = LaptopSpecs(
                brand="ASUS",
                family="TUF",
                gpu="RTX 4070",
                condition=Condition.LIKE_NEW,
            )
            for idx, price in enumerate(
                [120_000, 122_000, 124_000, 126_000, 128_000], start=1
            ):
                store.record_listing(
                    avito_id=idx,
                    title=f"clean {idx}",
                    seller_id=None,
                    url=None,
                    price=price,
                    specs=clean,
                    risk_score=0,
                )
            store.record_listing(
                avito_id=90,
                title="credit bait",
                seller_id=None,
                url=None,
                price=60_000,
                specs=clean,
                risk_score=30,
            )
            store.record_listing(
                avito_id=91,
                title="used",
                seller_id=None,
                url=None,
                price=70_000,
                specs=used,
                risk_score=0,
            )
            store.record_listing(
                avito_id=92,
                title="like new",
                seller_id=None,
                url=None,
                price=80_000,
                specs=like_new,
                risk_score=0,
            )

            stats = store.market_stats(clean, min_samples=5)
            self.assertEqual(stats.sample_size, 5)
            self.assertEqual(stats.median_price, 124_000)

    def test_fast_new_listing_does_not_enter_market_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DealWatcherStore(Path(tmp) / "test.db")
            specs = LaptopSpecs(
                brand="Lenovo",
                gpu="RTX 5060",
                condition=Condition.NEW_LIKELY,
            )
            for idx, price in enumerate(
                [120_000, 122_000, 124_000, 126_000, 128_000], start=1
            ):
                store.record_listing(
                    avito_id=idx,
                    title=f"market {idx}",
                    seller_id=None,
                    url=None,
                    price=price,
                    specs=specs,
                    baseline_eligible=True,
                )
            store.record_listing(
                avito_id=99,
                title="FAST bargain",
                seller_id=None,
                url=None,
                price=80_000,
                specs=specs,
                baseline_eligible=False,
            )
            stats = store.market_stats(specs, min_samples=5)
            self.assertEqual(stats.sample_size, 5)
            self.assertEqual(stats.median_price, 124_000)

            store.record_listing(
                avito_id=99,
                title="same listing later seen by MARKET",
                seller_id=None,
                url=None,
                price=80_000,
                specs=specs,
                baseline_eligible=True,
            )
            promoted = store.market_stats(specs, min_samples=5)
            self.assertEqual(promoted.sample_size, 6)

    def test_seen_state_is_scoped_by_profile_and_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DealWatcherStore(Path(tmp) / "test.db")
            store.mark_seen("market", [(10, 100_000)])
            self.assertTrue(store.is_seen("market", 10, 100_000))
            self.assertFalse(store.is_seen("fast", 10, 100_000))
            self.assertFalse(store.is_seen("market", 10, 95_000))


if __name__ == "__main__":
    unittest.main()
