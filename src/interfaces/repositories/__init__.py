"""Repository interface definitions."""

from .ai import AIRepository
from .analysis_db import AnalysisDBRepository
from .config import ConfigRepository
from .diary import DiaryRepository
from .market_data import MarketDataRepository
from .news import NewsRepository
from .portfolio import PortfolioRepository

__all__ = [
    "AIRepository",
    "AnalysisDBRepository",
    "ConfigRepository",
    "DiaryRepository",
    "MarketDataRepository",
    "NewsRepository",
    "PortfolioRepository",
]
