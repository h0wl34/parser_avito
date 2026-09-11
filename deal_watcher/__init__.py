"""Laptop deal intelligence built on top of the upstream Avito parser."""

from .config import DealWatcherConfig, load_deal_watcher_config
from .models import Condition, DealAnalysis, LaptopSpecs, MarketStats, RiskAssessment
from .service import DealWatcherService

__all__ = [
    "Condition",
    "DealAnalysis",
    "DealWatcherConfig",
    "DealWatcherService",
    "LaptopSpecs",
    "MarketStats",
    "RiskAssessment",
    "load_deal_watcher_config",
]
