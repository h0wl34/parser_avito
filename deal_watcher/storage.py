from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import statistics

from .models import LaptopSpecs, MarketStats


class DealWatcherStore:
    """SQLite storage isolated from upstream's viewed table."""

    def __init__(self, db_path: str | Path = "database.db"):
        self.db_path = str(db_path)
        self._ensure_schema()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS deal_listings (
                    avito_id INTEGER PRIMARY KEY,
                    title TEXT,
                    seller_id TEXT,
                    url TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    published_at TEXT,
                    brand TEXT,
                    family TEXT,
                    sku TEXT,
                    cpu TEXT,
                    gpu TEXT,
                    ram_gb INTEGER,
                    storage_gb INTEGER,
                    condition TEXT,
                    source_url TEXT
                );

                CREATE TABLE IF NOT EXISTS deal_prices (
                    avito_id INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    PRIMARY KEY (avito_id, observed_at, price)
                );

                CREATE TABLE IF NOT EXISTS deal_seen (
                    profile TEXT NOT NULL,
                    avito_id INTEGER NOT NULL,
                    price INTEGER NOT NULL,
                    seen_at TEXT NOT NULL,
                    PRIMARY KEY (profile, avito_id, price)
                );

                CREATE INDEX IF NOT EXISTS idx_deal_prices_time
                    ON deal_prices(observed_at);
                CREATE INDEX IF NOT EXISTS idx_deal_listings_gpu
                    ON deal_listings(gpu);
                CREATE INDEX IF NOT EXISTS idx_deal_listings_family_gpu
                    ON deal_listings(family, gpu);
                CREATE INDEX IF NOT EXISTS idx_deal_listings_sku_gpu
                    ON deal_listings(sku, gpu);
                """
            )

    def record_listing(
        self,
        *,
        avito_id: int,
        title: str,
        seller_id: str | None,
        url: str | None,
        price: int,
        specs: LaptopSpecs,
        source_url: str | None = None,
        published_at: datetime | None = None,
        observed_at: datetime | None = None,
    ) -> float | None:
        observed_at = observed_at or datetime.now(timezone.utc)
        stamp = observed_at.isoformat()
        published = published_at.isoformat() if published_at else None

        with self._connection() as conn:
            previous = conn.execute(
                "SELECT price FROM deal_prices "
                "WHERE avito_id = ? ORDER BY observed_at DESC LIMIT 1",
                (avito_id,),
            ).fetchone()

            conn.execute(
                """
                INSERT INTO deal_listings (
                    avito_id, title, seller_id, url, first_seen, last_seen,
                    published_at, brand, family, sku, cpu, gpu, ram_gb,
                    storage_gb, condition, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(avito_id) DO UPDATE SET
                    title=excluded.title,
                    seller_id=excluded.seller_id,
                    url=excluded.url,
                    last_seen=excluded.last_seen,
                    brand=excluded.brand,
                    family=excluded.family,
                    sku=excluded.sku,
                    cpu=excluded.cpu,
                    gpu=excluded.gpu,
                    ram_gb=excluded.ram_gb,
                    storage_gb=excluded.storage_gb,
                    condition=excluded.condition,
                    source_url=excluded.source_url
                """,
                (
                    avito_id,
                    title,
                    seller_id,
                    url,
                    stamp,
                    stamp,
                    published,
                    specs.brand,
                    specs.family,
                    specs.sku,
                    specs.cpu,
                    specs.gpu,
                    specs.ram_gb,
                    specs.storage_gb,
                    specs.condition.value,
                    source_url,
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO deal_prices "
                "(avito_id, observed_at, price) VALUES (?, ?, ?)",
                (avito_id, stamp, price),
            )

        if previous and previous["price"] and previous["price"] > price:
            return (previous["price"] - price) / previous["price"] * 100
        return None

    def is_seen(self, profile: str, avito_id: int, price: int) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM deal_seen
                WHERE profile = ? AND avito_id = ? AND price = ?
                """,
                (profile, avito_id, price),
            ).fetchone()
        return row is not None

    def mark_seen(
        self,
        profile: str,
        records: list[tuple[int, int]],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        if not records:
            return
        stamp = (observed_at or datetime.now(timezone.utc)).isoformat()
        rows = [
            (profile, avito_id, price, stamp)
            for avito_id, price in records
        ]
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO deal_seen
                    (profile, avito_id, price, seen_at)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )

    def market_stats(
        self,
        specs: LaptopSpecs,
        *,
        window_days: int = 30,
        min_samples: int = 5,
        exclude_avito_id: int | None = None,
    ) -> MarketStats:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

        for level, key in specs.market_key_candidates:
            where, params = self._where_for_key(level, key)
            sql = f"""
                SELECT dp.price
                FROM deal_prices dp
                JOIN deal_listings dl ON dl.avito_id = dp.avito_id
                WHERE dp.observed_at >= ?
                  AND dp.observed_at = (
                      SELECT MAX(dp2.observed_at)
                      FROM deal_prices dp2
                      WHERE dp2.avito_id = dp.avito_id
                  )
                  AND {where}
            """
            args: list[object] = [cutoff, *params]
            if exclude_avito_id is not None:
                sql += " AND dp.avito_id != ?"
                args.append(exclude_avito_id)

            with self._connection() as conn:
                prices = [
                    row["price"]
                    for row in conn.execute(sql, args).fetchall()
                    if row["price"] > 0
                ]

            if len(prices) >= min_samples:
                return self._summarize(prices, level)

        return MarketStats()

    @staticmethod
    def _where_for_key(level: str, key: str) -> tuple[str, list[str]]:
        if level == "sku_gpu":
            sku, gpu = key.split("|", 1)
            return "dl.sku = ? AND dl.gpu = ?", [sku, gpu]
        if level == "family_gpu":
            family, gpu = key.split("|", 1)
            return "dl.family = ? AND dl.gpu = ?", [family, gpu]
        if level == "brand_gpu":
            brand, gpu = key.split("|", 1)
            return "dl.brand = ? AND dl.gpu = ?", [brand, gpu]
        if level == "gpu":
            return "dl.gpu = ?", [key]
        raise ValueError(f"Unsupported market level: {level}")

    @staticmethod
    def _percentile(sorted_values: list[int], percentile: float) -> int:
        if not sorted_values:
            raise ValueError("empty values")
        index = (len(sorted_values) - 1) * percentile
        lower = int(index)
        upper = min(lower + 1, len(sorted_values) - 1)
        if lower == upper:
            return sorted_values[lower]
        fraction = index - lower
        return round(
            sorted_values[lower]
            + (sorted_values[upper] - sorted_values[lower]) * fraction
        )

    @classmethod
    def _summarize(cls, prices: list[int], level: str) -> MarketStats:
        values = sorted(prices)
        return MarketStats(
            sample_size=len(values),
            median_price=round(statistics.median(values)),
            p25_price=cls._percentile(values, 0.25),
            p10_price=cls._percentile(values, 0.10),
            min_price=values[0],
            comparison_level=level,
        )
