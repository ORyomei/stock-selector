"""Diary repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class DiaryRepository(ABC):
    """分析レポート・シグナル・トレード記録の永続化を抽象化するリポジトリ。"""

    @abstractmethod
    def save_report(self, filename: str, content: str) -> Path:
        """分析レポート (Markdown) を保存する。"""

    @abstractmethod
    def list_reports(self, days: int = 30) -> list[Path]:
        """直近 N 日分のレポートファイル一覧を返す。"""

    @abstractmethod
    def save_signal(self, filename: str, signal_data: dict[str, Any]) -> str:
        """売買シグナル JSON を保存する。"""

    @abstractmethod
    def load_signal(self, path: str | Path) -> dict[str, Any]:
        """指定パスのシグナル JSON を読み込む。"""

    @abstractmethod
    def list_signals(self) -> list[Path]:
        """全シグナルファイル一覧を返す。"""

    @abstractmethod
    def save_trade(self, trade_data: dict[str, Any]) -> str:
        """取引結果 JSON を保存する。"""

    @abstractmethod
    def load_recent_trades(self, days: int = 30) -> list[dict[str, Any]]:
        """直近 N 日分の取引結果を返す。"""

    def append_signal_log(self, record: dict[str, Any]) -> None:  # noqa: B027
        """シグナル結果 (採用/却下/スキップ) を追記ログに記録する。

        「却下したシグナルはその後どうなったか」の仮想追跡に使う。
        意図的に非抽象の no-op 既定 (追跡をサポートしない実装でも壊れない)。
        """

    def load_signal_log(self, days: int = 60) -> list[dict[str, Any]]:
        """直近 N 日分のシグナル結果ログを返す。既定は空。"""
        return []
