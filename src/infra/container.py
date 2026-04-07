"""DI コンテナ定義。

Usage::

    from infra.container import get_container

    container = get_container()
    market_data = container.market_data()
    portfolio = container.portfolio()
"""

from __future__ import annotations

from pathlib import Path

from dependency_injector import containers, providers

from infra.repositories.file_diary import FileDiaryRepository
from infra.repositories.google_news import GoogleNewsRepository
from infra.repositories.json_config import JsonConfigRepository
from infra.repositories.json_portfolio import JsonPortfolioRepository
from infra.repositories.litellm_ai import LiteLLMAIRepository
from infra.repositories.sqlite_analysis import SQLiteAnalysisRepository
from infra.repositories.yfinance_market_data import YFinanceMarketDataRepository

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_DIR / "config"
DIARY_DIR = PROJECT_DIR / "diary"
DATA_DIR = PROJECT_DIR / "data"
PORTFOLIO_FILE = PROJECT_DIR / "portfolio.json"
RISK_LIMITS_FILE = CONFIG_DIR / "risk_limits.json"
DB_FILE = DATA_DIR / "stock_analysis.db"


class RepositoryContainer(containers.DeclarativeContainer):
    """リポジトリの DI コンテナ。"""

    config = providers.Configuration()

    market_data = providers.Singleton(
        YFinanceMarketDataRepository,
        retries=2,
    )

    news = providers.Singleton(
        GoogleNewsRepository,
        retries=2,
    )

    ai = providers.Factory(
        LiteLLMAIRepository,
        provider=config.ai_provider,
        model=config.ai_model,
    )

    portfolio = providers.Singleton(
        JsonPortfolioRepository,
        portfolio_path=PORTFOLIO_FILE,
        risk_limits_path=RISK_LIMITS_FILE,
    )

    config_repo = providers.Singleton(
        JsonConfigRepository,
        config_dir=CONFIG_DIR,
    )

    diary = providers.Singleton(
        FileDiaryRepository,
        diary_dir=DIARY_DIR,
    )

    analysis_db = providers.Singleton(
        SQLiteAnalysisRepository,
        db_path=DB_FILE,
    )


_container: RepositoryContainer | None = None


def get_container(
    *,
    ai_provider: str = "copilot",
    ai_model: str | None = None,
) -> RepositoryContainer:
    """アプリケーション全体で共有するコンテナを取得する。"""
    global _container
    if _container is None:
        _container = RepositoryContainer()
        _container.config.ai_provider.from_value(ai_provider)
        _container.config.ai_model.from_value(ai_model)
    return _container


def reset_container() -> None:
    """テスト用: コンテナをリセットする。"""
    global _container
    _container = None
