"""ETF 判定の共有ヘルパー。

ETF には決算・財務データが存在せず、yfinance の quoteSummary 系 API を
叩くと 404 になる (1489.T で毎サイクル発生していた)。ユニバースに ETF を
追加したらここにも登録する。
"""

from __future__ import annotations

ETF_TICKERS: set[str] = {
    "2559.T",  # MAXIS 全世界株式 (オルカン)
    "1655.T",  # iシェアーズ S&P500
    "1306.T",  # NEXT FUNDS TOPIX
    "1545.T",  # NEXT FUNDS NASDAQ100
    "1489.T",  # NEXT FUNDS 日経高配当株50
}


def is_etf(ticker: str) -> bool:
    """既知の ETF なら True (決算/財務系 API の無駄撃ちを避ける判定)。"""
    return ticker in ETF_TICKERS
