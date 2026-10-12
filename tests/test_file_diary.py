"""diary/trades のファイル保存 — 同一秒・同一銘柄の連続保存で上書きしない。"""

import json
from datetime import UTC, datetime
from pathlib import Path

from infra.repositories import file_diary
from infra.repositories.file_diary import FileDiaryRepository


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 13, 1, 2, 3, tzinfo=UTC)


def test_save_trade_does_not_overwrite_same_second_same_ticker(tmp_path, monkeypatch):
    monkeypatch.setattr(file_diary, "datetime", _FixedDatetime)
    repo = FileDiaryRepository(diary_dir=tmp_path)
    p1 = repo.save_trade({"ticker": "1545.T", "action": "BUY"})
    p2 = repo.save_trade({"ticker": "1545.T", "action": "CLOSE"})
    p3 = repo.save_trade({"ticker": "1545.T", "action": "BUY"})
    assert len({p1, p2, p3}) == 3
    assert p1.endswith("2026-10-13_010203_1545.T_trade.json")
    assert p2.endswith("2026-10-13_010203_1545.T_2_trade.json")
    assert json.loads(Path(p1).read_text())["action"] == "BUY"
    assert json.loads(Path(p2).read_text())["action"] == "CLOSE"
    # 日付プレフィックスが保たれるので最近分の読み込みに全部含まれる
    actions = [t["action"] for t in repo.load_recent_trades(days=3650)]
    assert sorted(actions) == ["BUY", "BUY", "CLOSE"]
