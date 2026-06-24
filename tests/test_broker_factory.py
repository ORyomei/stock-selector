"""ブローカー生成ファクトリのリグレッションテスト。

カバー範囲:
- 全現金・0ポジションの保存状態を「ファイルなし」と誤判定して初期資金に
  リセットしてしまったバグ (factory.py の復元条件が positions の truthy 判定のみ
  だったため、positions=[] が falsy 扱いされ初期化されていた)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra.brokers.factory import create_broker  # noqa: E402
from infra.repositories.json_portfolio import JsonPortfolioRepository  # noqa: E402

_SIM_CONFIG = {"broker": "simulator", "simulator": {"initial_capital_jpy": 3_000_000, "initial_capital_usd": 0}}


class _FakeConfigRepo:
    """create_broker が必要とする load_trading_config のみを返す最小スタブ。"""

    def load_trading_config(self) -> dict[str, Any]:
        return _SIM_CONFIG

    def load_risk_limits(self) -> dict[str, Any]:
        return {}

    def load_watchlist(self) -> list[dict[str, Any]]:
        return []


def _repo(tmp_path) -> JsonPortfolioRepository:
    return JsonPortfolioRepository(tmp_path / "portfolio.json", tmp_path / "risk.json")


def test_restores_all_cash_zero_position_state(tmp_path):
    """positions=[] でも balance があれば復元し、初期資金にリセットしない。"""
    repo = _repo(tmp_path)
    repo.save(
        {
            "metadata": {"broker": "simulator"},
            # 過去益込みの全現金状態 (初期資金 3,000,000 より大きい)
            "balance": {"cash_jpy": 3_090_214.13, "cash_usd": 0.0, "timestamp": "2026-06-01T23:37:05+00:00"},
            "positions": [],
            "orders": {"pending": [], "filled": []},
        }
    )

    broker = create_broker(config_repo=_FakeConfigRepo(), portfolio_repo=repo)  # type: ignore[arg-type]

    bal = broker.get_balance()
    assert bal["cash_jpy"] == 3_090_214.13  # 初期資金 3,000,000 にリセットされていない
    assert broker.get_positions() == []


def test_fresh_when_no_file(tmp_path):
    """ファイルが無い時だけ初期資金で開始する。"""
    broker = create_broker(config_repo=_FakeConfigRepo(), portfolio_repo=_repo(tmp_path))  # type: ignore[arg-type]
    assert broker.get_balance()["cash_jpy"] == 3_000_000
    assert broker.get_positions() == []


def test_restores_open_positions(tmp_path):
    """建玉ありの状態も従来どおり復元される (回帰防止)。"""
    repo = _repo(tmp_path)
    repo.save(
        {
            "metadata": {"broker": "simulator"},
            "balance": {"cash_jpy": 500_000, "cash_usd": 0.0, "timestamp": "2026-06-01T00:00:00+00:00"},
            "positions": [
                {
                    "ticker": "1306.T",
                    "quantity": 1300,
                    "entry_price": 408.9,
                    "current_price": 429.7,
                    "entry_time": "2026-06-10T00:00:00+00:00",
                }
            ],
            "orders": {"pending": [], "filled": []},
        }
    )
    broker = create_broker(config_repo=_FakeConfigRepo(), portfolio_repo=repo)  # type: ignore[arg-type]
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].ticker == "1306.T"
    assert positions[0].quantity == 1300
