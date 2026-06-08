"""ポートフォリオ永続化のリグレッションテスト。

カバー範囲:
- portfolio_ops のフラット⇔ネスト往復 (CLI buy/sell でスキーマ破壊→建玉消失したバグ)
- JsonPortfolioRepository のアトミック書き込み + 破損時フォールバック
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import portfolio_ops  # noqa: E402
from infra.repositories.json_portfolio import JsonPortfolioRepository  # noqa: E402

NESTED = {
    "metadata": {"broker": "sim"},
    "balance": {"cash_jpy": 1_000_000, "cash_usd": 5_000, "timestamp": "2026-06-08T00:00:00+00:00"},
    "positions": [
        {
            "ticker": "8306.T",
            "quantity": 500,
            "entry_price": 3180.0,
            "current_price": 3165.0,
            "entry_time": "2026-06-04T05:31:00+00:00",
            "stop_loss": 2977.0,
            "take_profit": 3507.0,
        }
    ],
    "orders": {"pending": [], "filled": []},
}


def test_denormalize_preserves_positions_and_schema():
    """load→buy→save の往復後も positions/balance キーが残り、建玉が消えない。"""
    flat = portfolio_ops._normalize(json.loads(json.dumps(NESTED)))
    # 買い増し相当でフラット側を更新
    flat["holdings"][0]["shares"] = 600
    flat["cash_jpy"] = 800_000

    nested = portfolio_ops._denormalize(flat)

    assert "positions" in nested and "balance" in nested
    assert "holdings" not in nested  # フラットキーが漏れていない
    assert len(nested["positions"]) == 1
    pos = nested["positions"][0]
    assert pos["ticker"] == "8306.T"
    assert pos["quantity"] == 600  # 更新が反映
    assert pos["current_price"] == 3165.0  # 既存 current_price を温存
    assert pos["entry_time"] == "2026-06-04T05:31:00+00:00"  # 既存 entry_time を温存
    assert nested["balance"]["cash_jpy"] == 800_000
    assert nested["balance"]["cash_usd"] == 5_000  # 触っていない USD 現金を温存


def test_denormalize_is_idempotent_on_nested():
    """既にネスト形式 (holdings なし) の dict はそのまま返す。"""
    assert portfolio_ops._denormalize(json.loads(json.dumps(NESTED))) == NESTED


def test_round_trip_through_repo(tmp_path):
    """repo.save(_denormalize(...)) → load → _normalize で建玉が保持される。"""
    repo = JsonPortfolioRepository(tmp_path / "portfolio.json", tmp_path / "risk.json")
    repo.save(NESTED)

    # リーダーが positions を正しく読める (旧バグでは 0 件になっていた)
    assert repo.count_positions() == 1
    assert repo.get_held_tickers() == {"8306.T"}

    flat = portfolio_ops._normalize(repo.load())
    nested2 = portfolio_ops._denormalize(flat)
    repo.save(nested2)
    assert repo.count_positions() == 1  # 往復しても消えない


def test_save_is_atomic_and_recoverable(tmp_path):
    """save 後に本体を破損させても .bak から load できる。"""
    path = tmp_path / "portfolio.json"
    repo = JsonPortfolioRepository(path, tmp_path / "risk.json")
    repo.save(NESTED)
    repo.save({**NESTED, "metadata": {"v": 2}})  # 2回目で .bak が作られる

    # 本体を壊す
    path.write_text("{ broken json", encoding="utf-8")
    loaded = repo.load()  # .bak にフォールバック
    assert loaded is not None
    assert loaded["positions"][0]["ticker"] == "8306.T"
