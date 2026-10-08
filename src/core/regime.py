#!/usr/bin/env python3
"""レジームゲート (issue #19)。

稼働 10 週 41 建玉の総括 (diary/retro/2026-10-08_retrospective.md) で、新規エントリーの
損益は「建玉時に TOPIX が上昇トレンド中だったか」でほぼ決まっていた:

    TOPIX > 20 日線 かつ 5 日騰落 > 0 の局面で建玉  21 件  勝率 52%  +92,790 円
    それ以外の局面で建玉                            20 件  勝率 35%  -120,761 円

LLM に相場判断を委ねず、指数の測定値で「新規エントリーを許す局面か」を機械判定する。

- 判定は純関数 ``evaluate_regime`` (テスト可能)。データ取得は ``fetch_regime``
- 取得失敗・データ不足はフェイルオープン (ok=True + 警告)。ゲートの不具合で売買が
  永久停止するより従来動作に戻る方が安全
- yfinance の 1306.T 日足は直近バーが欠ける / NaN になることがある (daily-eval で既知)。
  日足末尾より新しい日付は時間足の日内最終値で補完する
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any

DEFAULT_TICKER = "1306.T"
DEFAULT_SMA_WINDOW = 20
DEFAULT_LOOKBACK_DAYS = 5
OUTLIER_FACTOR = 3.0


@dataclass
class RegimeSnapshot:
    ok: bool  # 新規エントリーを許可するか
    enabled: bool  # ゲートが有効か (無効なら常に ok=True)
    ticker: str
    close: float | None
    sma: float | None
    sma_window: int
    sma_gap_pct: float | None  # close / sma - 1 (%)
    lookback_ret_pct: float | None  # close / close[-lookback] - 1 (%)
    lookback_days: int
    as_of: str | None  # 判定に使った最新バーの日付
    reason: str

    @property
    def decided(self) -> bool:
        """測定値に基づいて判定できたか (False = フェイルオープン or 無効)。"""
        return self.enabled and self.close is not None and self.sma is not None

    def summary(self) -> str:
        if not self.enabled:
            return "ゲート無効"
        if not self.decided:
            return f"判定不能 → フェイルオープン ({self.reason})"
        verdict = "成立" if self.ok else "不成立"
        return (
            f"{verdict} — {self.ticker} {self.close:.1f} vs SMA{self.sma_window} {self.sma:.1f} "
            f"({self.sma_gap_pct:+.1f}%), {self.lookback_days}日騰落 {self.lookback_ret_pct:+.1f}%"
            f" [{self.as_of or '?'}] {self.reason}"
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def filter_outliers(closes: Sequence[float], factor: float = OUTLIER_FACTOR) -> list[float]:
    """中央値から factor 倍以上乖離した値と非数値を除外する (yfinance の +1051% 事故対策)。"""
    vals: list[float] = []
    for c in closes:
        try:
            v = float(c)
        except (TypeError, ValueError):
            continue
        if v == v and v > 0:  # NaN と非正値を除外
            vals.append(v)
    if len(vals) < 3:
        return vals
    med = median(vals)
    return [v for v in vals if med / factor <= v <= med * factor]


def evaluate_regime(
    closes: Sequence[float],
    *,
    ticker: str = DEFAULT_TICKER,
    sma_window: int = DEFAULT_SMA_WINDOW,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    as_of: str | None = None,
    enabled: bool = True,
) -> RegimeSnapshot:
    """終値列 (古い → 新しい) からレジームを判定する純関数。

    成立条件: close > SMA(sma_window) かつ close / close[-lookback_days] > 1。
    データ不足は判定不能 = フェイルオープン (ok=True)。
    """
    base: dict[str, Any] = {
        "ticker": ticker,
        "sma_window": sma_window,
        "lookback_days": lookback_days,
        "as_of": as_of,
        "enabled": enabled,
    }
    empty = {"close": None, "sma": None, "sma_gap_pct": None, "lookback_ret_pct": None}
    if not enabled:
        return RegimeSnapshot(ok=True, reason="ゲート無効", **empty, **base)

    vals = filter_outliers(closes)
    need = max(sma_window, lookback_days + 1)
    if len(vals) < need:
        return RegimeSnapshot(
            ok=True, reason=f"データ不足 ({len(vals)} 本 < {need} 本)", **empty, **base
        )

    close = vals[-1]
    sma = sum(vals[-sma_window:]) / sma_window
    ref = vals[-1 - lookback_days]
    sma_gap = (close / sma - 1) * 100
    ret = (close / ref - 1) * 100
    above = close > sma
    rising = ret > 0
    ok = above and rising
    if ok:
        reason = "上昇トレンド中"
    elif not above and not rising:
        reason = f"SMA{sma_window} 割れ かつ {lookback_days}日騰落マイナス"
    elif not above:
        reason = f"SMA{sma_window} 割れ"
    else:
        reason = f"{lookback_days}日騰落マイナス"
    return RegimeSnapshot(
        ok=ok, close=close, sma=sma, sma_gap_pct=sma_gap, lookback_ret_pct=ret,
        reason=reason, **base,
    )


def load_regime_config() -> dict[str, Any]:
    """config/risk_limits.json の ``regime_gate`` ブロック (無ければ空 = 既定値)。"""
    try:
        from core.trade import load_risk_limits

        cfg = load_risk_limits().get("regime_gate", {})
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def daily_closes_with_intraday_fill(market_data: Any, ticker: str) -> tuple[list[float], str | None]:
    """日足終値 (3mo) に、日足末尾より新しい日の時間足終値を補完して返す。

    戻り値は (終値列, 最新バーの日付 ISO)。日足が取れなければ ([], None)。
    """
    hist = market_data.get_price_history(ticker, period="3mo", interval="1d")
    if hist is None or hist.empty or "Close" not in hist.columns:
        return [], None
    daily = hist["Close"].dropna()
    if daily.empty:
        return [], None
    closes = [float(v) for v in daily.tolist()]
    last_date = daily.index[-1].date()
    try:
        intra = market_data.get_price_history(ticker, period="5d", interval="1h")
        if intra is not None and not intra.empty and "Close" in intra.columns:
            s = intra["Close"].dropna()
            per_day = s.groupby(s.index.date).last()
            for d, v in per_day.items():
                if d > last_date:
                    closes.append(float(v))
                    last_date = d
    except Exception as e:  # 補完は任意。日足だけで判定を続ける
        print(f"⚠️  regime: 時間足の補完に失敗 (日足のみで判定): {e}", file=sys.stderr)
    return closes, last_date.isoformat()


def fetch_regime(cfg: dict[str, Any] | None = None) -> RegimeSnapshot:
    """設定に従って指数データを取得し判定する。失敗はフェイルオープン。"""
    cfg = load_regime_config() if cfg is None else cfg
    enabled = bool(cfg.get("enabled", True))
    ticker = str(cfg.get("ticker", DEFAULT_TICKER))
    sma_window = int(cfg.get("sma_window", DEFAULT_SMA_WINDOW))
    lookback = int(cfg.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    kw: dict[str, Any] = {"ticker": ticker, "sma_window": sma_window, "lookback_days": lookback}
    if not enabled:
        return evaluate_regime([], enabled=False, **kw)
    try:
        from infra.container import get_container

        closes, as_of = daily_closes_with_intraday_fill(get_container().market_data(), ticker)
    except Exception as e:
        print(f"⚠️  regime fetch failed (fail-open): {e}", file=sys.stderr)
        return RegimeSnapshot(
            ok=True, enabled=True, close=None, sma=None, sma_gap_pct=None,
            lookback_ret_pct=None, as_of=None, reason=f"取得失敗: {e}", **kw,
        )
    return evaluate_regime(closes, as_of=as_of, **kw)
