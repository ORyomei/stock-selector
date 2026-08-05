#!/usr/bin/env python3
"""経済イベントカレンダー (取得 + 入れ子 AI 抽出)。

FOMC・日銀会合・米CPI・雇用統計・SQ 等の「予定」を返す。check_macro は指標の
現在値しか見ないため、これが無いとイベント直前でも平然と新規買いできてしまう。

deep_research と同じ「HTML 取得 → 入れ子 LLM で構造化」パターン。カレンダー
ページは過去実績と予定が混在した崩れたテキストになるため、決定的パーサより
AI 抽出の方がレイアウト変更に頑健。AI が失敗しても生テキスト抜粋を返す。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agents.ai import call_ai, parse_ai_json  # noqa: E402

_JST = timezone(timedelta(hours=9))
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
_SOURCE_URL = "https://fx.minkabu.jp/indicators"
_FETCH_TIMEOUT = 10
_TEXT_CHARS = 16000  # AI に渡すページテキストの上限
_CACHE_PATH = SRC_DIR.parent / "data" / "market_calendar_cache.json"
_CACHE_TTL_SEC = 3 * 3600  # 予定は日中ほぼ変わらない。30分サイクル毎の入れ子AI再実行を防ぐ


def _cache_load(days: int, now: datetime) -> dict[str, Any] | None:
    try:
        d = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(d["_fetched_at"])
        if d.get("window_days") == days and (now - fetched).total_seconds() < _CACHE_TTL_SEC:
            out = {k: v for k, v in d.items() if k != "_fetched_at"}
            out["cache"] = {"hit": True, "fetched_at_jst": fetched.strftime("%Y-%m-%d %H:%M")}
            return out
    except Exception:
        pass
    return None


def _cache_save(result: dict[str, Any], now: datetime) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps({**result, "_fetched_at": now.isoformat()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _fetch_calendar_text() -> str:
    """カレンダーページの本文テキスト。失敗時は空文字。"""
    from bs4 import BeautifulSoup

    try:
        r = requests.get(_SOURCE_URL, headers=_UA, timeout=_FETCH_TIMEOUT)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript"]):
            tag.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:_TEXT_CHARS]
    except Exception:
        return ""


def _build_prompt(text: str, now: datetime, days: int) -> str:
    return "\n".join([
        f"現在は {now.strftime('%Y-%m-%d %H:%M')} JST です。",
        f"以下は経済指標カレンダーページのテキストです。今後 {days} 日以内に予定されている",
        "市場イベントを抽出してください。過去の発表結果 (実績値が既に載っているもの) は除外。",
        "",
        "特に重要: FOMC・日銀金融政策決定会合・米CPI・米雇用統計・米PCE・GDP速報・",
        "メジャーSQ・日銀総裁/FRB議長発言。ページに無いイベントを推測で足さないこと。",
        "**株式市場を動かしうる importance が medium 以上のイベントだけ**を出力し、",
        "細かい低重要度の指標 (在庫統計・地域指標など) は省くこと。",
        "",
        "## ページテキスト",
        text,
        "",
        "## 出力形式 (JSON のみ)",
        json.dumps({
            "events": [{
                "datetime_jst": "YYYY-MM-DD HH:MM",
                "country": "日本|アメリカ|ユーロ圏|その他",
                "name": "イベント名",
                "importance": "high|medium|low",
                "market_impact": "株式市場への影響を1文で",
            }],
            "trading_caution": "今後の売買で注意すべき点を1〜2文 (該当イベントが無ければ空文字)",
        }, ensure_ascii=False, indent=2),
    ])


def run_market_calendar(
    days: int = 7,
    provider: str = "claude_code",
    model: str | None = "sonnet",
) -> dict[str, Any]:
    """今後 days 日の経済イベント予定を返す。

    Returns:
        events (AI 抽出) / trading_caution / ai (入れ子呼び出しの記録)。
        AI が失敗した場合は生テキスト抜粋 (raw_excerpt) を返してツールとしては成立させる。
    """
    now = datetime.now(_JST)
    cached = _cache_load(days, now)
    if cached is not None:
        return cached

    text = _fetch_calendar_text()
    result: dict[str, Any] = {
        "as_of_jst": now.strftime("%Y-%m-%d %H:%M"),
        "window_days": days,
        "source": _SOURCE_URL,
    }
    if not text:
        result["events"] = []
        result["ai"] = {"used": False, "reason": "カレンダーページを取得できませんでした"}
        return result

    ai_meta: dict[str, Any] = {
        "used": False,
        "provider": provider,
        "model": model or "default",
        "prompt_chars": len(text),
    }
    try:
        raw = call_ai(
            _build_prompt(text, now, days),
            provider,
            model,
            system_msg="経済カレンダーから市場イベント予定を抽出するAI。JSONのみで回答。",
        )
        parsed = parse_ai_json(raw)
        if parsed and isinstance(parsed.get("events"), list):
            ai_meta["used"] = True
            ai_meta["response_chars"] = len(raw or "")
            result["events"] = parsed["events"]
            result["trading_caution"] = parsed.get("trading_caution", "")
        else:
            ai_meta["error"] = "JSON をパースできませんでした"
            result["events"] = []
            result["raw_excerpt"] = text[:2000]
    except Exception as e:
        ai_meta["error"] = str(e)
        result["events"] = []
        result["raw_excerpt"] = text[:2000]

    result["ai"] = ai_meta
    if ai_meta["used"]:  # 成功時のみキャッシュ (失敗を3時間固定しない)
        _cache_save(result, now)
    return result


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    print(json.dumps(run_market_calendar(days), ensure_ascii=False, indent=2))
