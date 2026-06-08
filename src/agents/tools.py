"""LangGraph tool definitions wrapping existing analysis scripts.

Each tool delegates to an existing script function, catching errors
and returning a structured dict that the LLM can consume.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

SRC_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SRC_DIR))


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _safe(fn, *a, **kw) -> dict[str, Any]:
    """Run *fn* and return its result, or an error dict on failure."""
    try:
        result = fn(*a, **kw)
        if result is None:
            return {"error": "データが取得できませんでした"}
        return result
    except SystemExit:
        return {"error": "データ不足のため分析できませんでした"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def check_macro() -> dict[str, Any]:
    """マクロ経済指標を取得。VIX・米10年金利・ドル円・原油・金・主要株価指数の現在値と変化率、
    市場環境スコア（リスクオン/オフ判定）を返す。分析の最初に呼んで市場全体の状況を把握する。"""
    from core.macro import fetch_macro
    return _safe(fetch_macro)


@tool
def screen_stocks(
    market: str = "all",
    strategy: str = "all",
    top: int = 5,
    universe_size: str = "default",
) -> dict[str, Any]:
    """市場全体から有望銘柄をスクリーニング。
    market: 'us'(米国株) / 'jp'(日本株) / 'all'(全市場)
    strategy: 'oversold'(売られすぎ) / 'momentum'(モメンタム) / 'breakout'(ブレイクアウト) / 'value'(バリュー) / 'all'
    top: 各戦略の上位N件
    universe_size: 'default' / 'expanded'(S&P500+日経225全銘柄)"""
    from core.screener import run_screen
    return _safe(run_screen, market=market, strategy=strategy, top=top, universe_size=universe_size)


@tool
def score_stock(ticker: str, period: str = "6mo") -> dict[str, Any]:
    """銘柄のテクニカルスコアを算出。RSI/MACD/ボリンジャーバンド/トレンド/出来高で -100〜+100 の
    総合スコア、上昇確率、目標価格、エントリーポイント、損切り/利確ラインを返す。
    ticker: ティッカーシンボル (例: AAPL, 7203.T)
    period: 分析期間 (例: 3mo, 6mo, 1y)"""
    from core.scorer import compute_score
    return _safe(compute_score, ticker, period)


@tool
def analyze_fundamentals(ticker: str) -> dict[str, Any]:
    """銘柄のファンダメンタル分析。PER/PBR/ROE/売上成長率/負債比率/FCF/配当/アナリスト予想/
    決算サプライズを取得し、ファンダメンタルスコア（最大70点）を返す。
    ticker: ティッカーシンボル (例: AAPL, 7203.T)"""
    from core.fundamentals import analyze_fundamentals as _analyze
    return _safe(_analyze, ticker)


@tool
def check_sentiment(query: str, limit: int = 20) -> dict[str, Any]:
    """ニュースヘッドラインのセンチメント分析。Google News から日英両方のヘッドラインを取得し、
    ポジティブ/ネガティブ/ニュートラルの割合と平均スコア(-1〜+1)を返す。
    query: 検索クエリ (企業名やティッカー)
    limit: 取得ヘッドライン数"""
    from core.sentiment import run_sentiment
    result = _safe(run_sentiment, query, limit)
    if result is None:
        return {"error": "センチメントデータが取得できませんでした"}
    return result


@tool
def analyze_events(
    query: str | None = None,
    lang: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """イベント因果分析。地政学リスク・金利変動・AI関連・景気後退・関税・為替・エネルギーの
    7つのルールでニュースを分類し、セクター/資産への影響と注目銘柄を返す。
    query: 検索クエリ (Noneで自動マクロクエリ)
    lang: 言語 ('ja'/'en'/None)
    limit: 記事数"""
    from core.event_impact import run as run_event
    return _safe(run_event, query, lang, limit)


@tool
def get_technical(ticker: str, period: str = "6mo") -> dict[str, Any]:
    """テクニカル指標の生データを取得。RSI, MACD, ボリンジャーバンド, SMA/EMA と
    テクニカルシグナル（ゴールデンクロス等）を返す。score_stock より軽量。
    ticker: ティッカーシンボル
    period: 分析期間"""
    from core.technical import analyze
    return _safe(analyze, ticker, period)


@tool
def get_news(query: str, lang: str = "ja", limit: int = 10) -> dict[str, Any]:
    """ニュース記事を取得。Google News RSS からヘッドライン一覧を返す。
    センチメント分析なしの生ニュースが欲しい時に使う。
    query: 検索クエリ
    lang: 言語 ('ja'/'en')
    limit: 取得件数"""
    from core.news import fetch_news
    return _safe(fetch_news, query, lang, limit)


@tool
def get_prices(ticker: str, period: str = "3mo") -> dict[str, Any]:
    """株価データを取得。現在値・始値・高値・安値・出来高・時価総額・PER・PBR・
    配当利回り・52週高値/安値を返す。
    ticker: ティッカーシンボル
    period: 取得期間 (1d,5d,1mo,3mo,6mo,1y)"""
    from core.prices import fetch
    return _safe(fetch, ticker, period)


# ---------------------------------------------------------------------------
# All tools list (for graph construction)
# ---------------------------------------------------------------------------

ALL_TOOLS = [
    check_macro,
    screen_stocks,
    score_stock,
    analyze_fundamentals,
    check_sentiment,
    analyze_events,
    get_technical,
    get_news,
    get_prices,
]
