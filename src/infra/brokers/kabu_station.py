"""auカブコム証券 kabuステーション API 実装

kabuステーション (Windows アプリ) が起動した状態で、ローカルの REST API
(http://localhost:18080  本番 / :18081  検証) に対してリクエストを発行する。

公式ドキュメント:
  https://kabucom.github.io/kabusapi/ptal/

セットアップ:
  1. auカブコム証券 で口座開設し、kabuステーション をインストール
  2. kabuステーション の [ツール] → [API設定] で API を有効化、
     API パスワードを設定
  3. config/trading_config.json の "kabu" セクションを設定
  4. 環境変数 KABU_API_PASSWORD に API パスワードを設定
     (もしくは config に直接記述。本番運用では環境変数推奨)
  5. kabuステーション にログインした状態でこのコードを実行

注意:
  - 日本株のみ対応 (現物 / 信用)。米国株は非対応 (kabu API の仕様)
  - ティッカー形式: "7203.T" → symbol="7203", exchange=1 (東証)
  - kabuステーション本体が起動・ログイン済みでないと API は応答しない
  - 本番モードでは実際に発注されるため、十分にテストすること
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

import requests

from domain.models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from interfaces.broker import BrokerInterface

logger = logging.getLogger(__name__)


# 市場コード (Exchange)
EXCHANGE_TOSHO = 1  # 東証 (※照会用。新規発注には通常 SOR or 東証+ を使う)
EXCHANGE_MEISHO = 3  # 名証
EXCHANGE_FSE = 5  # 福証
EXCHANGE_SSE = 6  # 札証
EXCHANGE_SOR = 9  # SOR (発注時の推奨)
EXCHANGE_TOSHO_PLUS = 27  # 東証+ (発注時の推奨)

# 注文タイプ (FrontOrderType)
FRONT_ORDER_MARKET = 10  # 成行
FRONT_ORDER_LIMIT = 20  # 指値
FRONT_ORDER_STOP = 30  # 逆指値

# 売買区分 (Side): "1"=売, "2"=買
SIDE_SELL = "1"
SIDE_BUY = "2"

# 口座種別 (AccountType): 2=一般, 4=特定, 12=法人
ACCOUNT_TYPE_TOKUTEI = 4

# 取引区分 (CashMargin): 1=現物, 2=新規(信用), 3=返済(信用)
CASH_MARGIN_SPOT = 1

# 受渡区分 (DelivType): 0=指定なし(信用), 2=お預かり金, 3=auマネーコネクト
DELIV_TYPE_CASH = 2

# 資産区分 (FundType)
FUND_TYPE_DEFAULT = "  "  # 半角スペース2つ (売り時 / 信用時)
FUND_TYPE_MARGIN = "02"  # 保護区分 (現物買の既定値)

# 有効期限区分 (TimeInForce): 1=FAS, 2=FAK, 3=FOK
TIF_FAS = 1


class KabuStationError(Exception):
    """kabu API 呼び出し失敗時の例外"""


class KabuStationBroker(BrokerInterface):
    """auカブコム証券 kabuステーション API ブローカー

    Args:
        config: trading_config.json の "kabu" セクション
            - host: kabuステーション API ホスト (default: "localhost")
            - port: ポート番号 (本番: 18080, 検証: 18081)
            - api_password_env: API パスワードを格納した環境変数名
                                (default: "KABU_API_PASSWORD")
            - api_password: 環境変数を使わない場合に直接指定
            - default_exchange: 既定の市場コード (default: 1=東証)
            - account_type: 口座種別 (default: 4=特定)
            - sandbox: True なら検証環境 (port=18081)
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        host = config.get("host", "localhost")
        sandbox = config.get("sandbox", False)
        port = config.get("port", 18081 if sandbox else 18080)
        self.base_url = f"http://{host}:{port}/kabusapi"

        password_env = config.get("api_password_env", "KABU_API_PASSWORD")
        self.api_password = config.get("api_password") or os.environ.get(password_env)
        if not self.api_password:
            raise KabuStationError(
                f"kabu API パスワードが未設定です。環境変数 {password_env} か "
                f"trading_config.json の kabu.api_password に設定してください。"
            )

        # 発注用市場コード: SOR(9) が推奨。東証(1)は通常時の新規発注不可
        self.order_exchange = int(config.get("order_exchange", EXCHANGE_SOR))
        # 照会用市場コード (positions/board)
        self.default_exchange = int(config.get("default_exchange", EXCHANGE_TOSHO))
        self.account_type = config.get("account_type", ACCOUNT_TYPE_TOKUTEI)
        self.timeout = config.get("timeout", 10)

        self._token: str | None = None
        # 注文・ポジションのキャッシュ (sync_from_broker で更新)
        self._orders_cache: list[Order] = []
        self._positions_cache: list[Position] = []
        self._balance_cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 認証
    # ------------------------------------------------------------------
    def _ensure_token(self) -> str:
        """API トークン取得 (既に取得済みなら再利用)"""
        if self._token:
            return self._token
        url = f"{self.base_url}/token"
        try:
            resp = requests.post(
                url,
                json={"APIPassword": self.api_password},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            token = str(data["Token"])
            self._token = token
            logger.info("kabu API トークン取得成功")
            return token
        except requests.RequestException as exc:
            raise KabuStationError(
                f"kabu API トークン取得失敗: {exc}. "
                f"kabuステーションが起動・ログイン済みか確認してください。"
            ) from exc

    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self._ensure_token()}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = self._headers()
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
                timeout=self.timeout,
            )
            if resp.status_code == 401:
                # トークン失効 → 再取得して 1 回リトライ
                self._token = None
                headers = self._headers()
                resp = requests.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=self.timeout,
                )
            resp.raise_for_status()
            if resp.content:
                return resp.json()
            return None
        except requests.HTTPError as exc:
            body = exc.response.text if exc.response is not None else ""
            raise KabuStationError(
                f"kabu API {method} {path} 失敗: {exc} body={body}"
            ) from exc
        except requests.RequestException as exc:
            raise KabuStationError(f"kabu API 通信失敗: {exc}") from exc

    # ------------------------------------------------------------------
    # ティッカー変換
    # ------------------------------------------------------------------
    def _parse_ticker(self, ticker: str, *, for_order: bool = False) -> tuple[str, int]:
        """"7203.T" → ("7203", exchange) のように分解。

        Args:
            for_order: True なら発注用の市場コード (SOR等) を返す。
                       False なら照会用の市場コード (東証等) を返す。
        """
        if "." in ticker:
            symbol, suffix = ticker.split(".", 1)
            if for_order:
                # 発注時: 東証銘柄は SOR/東証+ を使う (東証直接は新規発注不可)
                order_mapping = {
                    "T": self.order_exchange,
                    "N": EXCHANGE_MEISHO,
                    "F": EXCHANGE_FSE,
                    "S": EXCHANGE_SSE,
                }
                exchange = order_mapping.get(suffix.upper(), self.order_exchange)
            else:
                # 照会時: 東証(1)でOK
                query_mapping = {
                    "T": EXCHANGE_TOSHO,
                    "N": EXCHANGE_MEISHO,
                    "F": EXCHANGE_FSE,
                    "S": EXCHANGE_SSE,
                }
                exchange = query_mapping.get(suffix.upper(), self.default_exchange)
            return symbol, exchange
        # 米国株などサフィックスなし → 非対応
        if not ticker.isdigit():
            raise KabuStationError(
                f"kabu API は日本株のみ対応です: {ticker} は発注できません"
            )
        return ticker, self.order_exchange if for_order else self.default_exchange

    # ------------------------------------------------------------------
    # BrokerInterface 実装
    # ------------------------------------------------------------------
    def managed_currencies(self) -> set[str]:
        """kabu は日本株 (現物) のみ取り扱う → JPY のみ管理。"""
        return {"JPY"}

    def get_trading_unit(self, ticker: str) -> int:
        """kabu の銘柄情報 (/symbol の TradingUnit) から売買単位を取得する。

        ETF は 1 口単位のことが多く、個別株 (100 株) 前提では誤るため
        実際の単位をブローカーに問い合わせる。取得失敗時はデフォルトに戻す。
        """
        try:
            symbol, exchange = self._parse_ticker(ticker, for_order=False)
            data = self._request("GET", f"/symbol/{symbol}@{exchange}")
            unit = int(data.get("TradingUnit", 0)) if data else 0
            if unit > 0:
                return unit
        except Exception as exc:
            logger.warning(f"get_trading_unit 失敗 ({ticker}): {exc}")
        return 100 if ticker.endswith(".T") else 1

    def get_balance(self) -> dict[str, Any]:
        """買付余力を取得。"""
        data = self._request("GET", "/wallet/cash")
        # 口座タイプにより値が入るフィールドが異なる
        cash_jpy = float(
            data.get("StockAccountWallet")
            or data.get("AuKCStockAccountWallet")
            or data.get("AuJbnStockAccountWallet")
            or 0
        )
        self._balance_cache = {
            "cash_jpy": cash_jpy,
            "cash_usd": 0.0,
            "timestamp": datetime.now(UTC),
        }
        return self._balance_cache

    def place_order(
        self,
        ticker: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        entry_price: float = 0.0,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> Order:
        """現物注文を発注する。"""
        symbol, exchange = self._parse_ticker(ticker, for_order=True)

        front_order_type = {
            OrderType.MARKET: FRONT_ORDER_MARKET,
            OrderType.LIMIT: FRONT_ORDER_LIMIT,
            OrderType.STOP: FRONT_ORDER_STOP,
        }.get(order_type, FRONT_ORDER_MARKET)

        kabu_side = SIDE_BUY if side == OrderSide.BUY else SIDE_SELL

        # FundType: 現物買=保護区分"02", 現物売="  "(半角スペース2つ)
        fund_type = FUND_TYPE_MARGIN if side == OrderSide.BUY else FUND_TYPE_DEFAULT

        body: dict[str, Any] = {
            "Symbol": symbol,
            "Exchange": exchange,
            "SecurityType": 1,  # 株式
            "Side": kabu_side,
            "CashMargin": CASH_MARGIN_SPOT,
            "DelivType": DELIV_TYPE_CASH if side == OrderSide.BUY else 0,
            "FundType": fund_type,
            "AccountType": self.account_type,
            "Qty": int(quantity),
            "FrontOrderType": front_order_type,
            "Price": float(entry_price) if order_type == OrderType.LIMIT else 0,
            "ExpireDay": 0,  # 当日
        }
        if order_type == OrderType.MARKET:
            body["Price"] = 0

        logger.info(f"kabu sendorder: {body}")
        data = self._request("POST", "/sendorder", json_body=body)

        if not data or data.get("Result", -1) != 0:
            raise KabuStationError(f"kabu 発注失敗: {data}")

        order_id = data.get("OrderId")
        if not order_id:
            # Result==0 でも OrderId が無い応答は追跡不能 → 偽 ID を採番せずエラーにする
            raise KabuStationError(f"kabu 発注応答に OrderId がありません: {data}")

        # sendorder は非同期。約定状況をポーリングして FILLED / 約定単価を反映する
        # (成行は通常即時約定。execute_signal は FILLED のみ success 扱いのため必須)
        return self._resolve_order(
            order_id,
            ticker=ticker,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            order_type=order_type,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    def _resolve_order(
        self,
        order_id: str,
        *,
        ticker: str,
        side: OrderSide,
        quantity: int,
        entry_price: float,
        order_type: OrderType,
        stop_loss: float | None,
        take_profit: float | None,
        poll: int = 5,
        interval: float = 0.6,
    ) -> Order:
        """sendorder 後に /orders を数回ポーリングして注文状態を解決する。

        約定完了なら FILLED + 加重平均約定単価、終了かつ未約定なら CANCELLED、
        解決できなければ PENDING を返す。
        """
        status = OrderStatus.PENDING
        fill_price: float | None = None
        filled_qty = 0
        for attempt in range(poll):
            try:
                rows = self._request("GET", "/orders", params={"id": str(order_id)}) or []
            except KabuStationError:
                rows = []
            row = rows[0] if rows else None
            if row:
                state = int(row.get("State", 0) or 0)
                order_qty = int(row.get("OrderQty", quantity) or quantity)
                filled_qty = int(row.get("CumQty", 0) or 0)
                fill_price = self._avg_fill_price(row.get("Details", []))
                if order_qty > 0 and filled_qty >= order_qty:
                    status = OrderStatus.FILLED
                    break
                if state == 5:  # 終了 (約定 or 取消・失効)
                    status = OrderStatus.FILLED if filled_qty > 0 else OrderStatus.CANCELLED
                    break
                if filled_qty > 0:
                    status = OrderStatus.PARTIALLY_FILLED
            if attempt < poll - 1:
                time.sleep(interval)

        return Order(
            id=str(order_id),
            ticker=ticker,
            side=side,
            quantity=quantity,
            entry_price=float(entry_price),
            order_type=order_type,
            order_time=datetime.now(UTC),
            filled_quantity=filled_qty,
            fill_price=fill_price,
            status=status,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    @staticmethod
    def _avg_fill_price(details: list[dict[str, Any]]) -> float | None:
        """Details 配列 (RecType==8 が約定) から加重平均約定単価を計算する。"""
        total_value = sum(
            float(d.get("Price", 0) or 0) * int(d.get("Qty", 0) or 0)
            for d in details
            if d.get("RecType") == 8
        )
        total_qty = sum(
            int(d.get("Qty", 0) or 0) for d in details if d.get("RecType") == 8
        )
        return total_value / total_qty if total_qty > 0 else None

    def cancel_order(self, order_id: str) -> bool:
        """注文キャンセル。"""
        try:
            data = self._request(
                "PUT",
                "/cancelorder",
                json_body={"OrderId": order_id},
            )
            return data.get("Result", -1) == 0 if data else False
        except KabuStationError as exc:
            logger.warning(f"cancel_order 失敗: {exc}")
            return False

    def get_orders(self) -> list[Order]:
        """未約定注文一覧。"""
        data = self._request("GET", "/orders", params={"product": "0"}) or []
        result: list[Order] = []
        for item in data:
            # State: 1=待機 2=処理中 3=処理済 4=訂正取消中 5=終了
            state = item.get("State", 0)
            if state == 5:
                # 終了した注文はスキップ (約定・取消・失効)
                continue

            # CumQty(累計約定数量) vs OrderQty で部分約定を判定
            order_qty = int(item.get("OrderQty", 0))
            cum_qty = int(item.get("CumQty", 0) or 0)
            if cum_qty >= order_qty and order_qty > 0:
                status = OrderStatus.FILLED
            elif cum_qty > 0:
                status = OrderStatus.PARTIALLY_FILLED
            else:
                status = OrderStatus.PENDING

            if status == OrderStatus.FILLED:
                continue

            # 約定価格は Details 配列から取得
            fill_price = self._avg_fill_price(item.get("Details", [])) if cum_qty > 0 else None

            ticker_str = f"{item.get('Symbol', '')}.T"
            side = OrderSide.BUY if str(item.get("Side")) == SIDE_BUY else OrderSide.SELL
            front_type = item.get("OrdType") or item.get("FrontOrderType", FRONT_ORDER_MARKET)
            order_type = (
                OrderType.LIMIT
                if front_type == FRONT_ORDER_LIMIT
                else OrderType.STOP
                if front_type == FRONT_ORDER_STOP
                else OrderType.MARKET
            )
            result.append(
                Order(
                    id=str(item.get("ID", item.get("OrderId", ""))),
                    ticker=ticker_str,
                    side=side,
                    quantity=order_qty,
                    entry_price=float(item.get("Price", 0) or 0),
                    order_type=order_type,
                    order_time=datetime.now(UTC),
                    filled_quantity=cum_qty,
                    fill_price=fill_price,
                    status=status,
                )
            )
        self._orders_cache = result
        return result

    def get_positions(self) -> list[Position]:
        """保有ポジション一覧（現物のみ）。"""
        # product=1: 現物のみ (2=信用, 3=先物, 4=OP)
        data = self._request("GET", "/positions", params={"product": "1"}) or []
        positions: list[Position] = []
        for item in data:
            qty = int(item.get("LeavesQty", 0))
            if qty <= 0:
                continue
            symbol = item.get("Symbol", "")
            ticker_str = f"{symbol}.T"
            positions.append(
                Position(
                    ticker=ticker_str,
                    quantity=qty,
                    entry_price=float(item.get("Price", 0) or 0),
                    current_price=float(item.get("CurrentPrice", 0) or 0),
                    entry_time=datetime.now(UTC),
                )
            )
        self._positions_cache = positions
        return positions

    def get_filled_orders(self, limit: int = 100) -> list[Order]:
        """約定済み注文の履歴を取得。"""
        data = self._request("GET", "/orders", params={"product": "0"}) or []
        result: list[Order] = []
        for item in data[:limit]:
            # State=5 (終了) かつ CumQty>0 → 約定
            state = item.get("State", 0)
            if state != 5:
                continue
            order_qty = int(item.get("OrderQty", 0))
            cum_qty = int(item.get("CumQty", 0) or 0)
            # 約定なしで終了 = 取消 or 失効
            status = OrderStatus.FILLED if cum_qty > 0 else OrderStatus.CANCELLED

            # 約定価格は Details から取得
            fill_price = self._avg_fill_price(item.get("Details", [])) if cum_qty > 0 else None

            ticker_str = f"{item.get('Symbol', '')}.T"
            side = OrderSide.BUY if str(item.get("Side")) == SIDE_BUY else OrderSide.SELL
            result.append(
                Order(
                    id=str(item.get("ID", item.get("OrderId", ""))),
                    ticker=ticker_str,
                    side=side,
                    quantity=order_qty,
                    entry_price=float(item.get("Price", 0) or 0),
                    order_type=OrderType.MARKET,
                    order_time=datetime.now(UTC),
                    filled_quantity=cum_qty,
                    fill_price=fill_price,
                    status=status,
                )
            )
        return result

    def sync_from_broker(self) -> None:
        """ブローカー側の最新状態を取得してキャッシュ更新。"""
        self.get_balance()
        self.get_positions()
        self.get_orders()

    # ------------------------------------------------------------------
    # 接続テスト
    # ------------------------------------------------------------------
    def ping(self) -> dict[str, Any]:
        """API 接続テスト (トークン取得 + 残高取得)。"""
        token = self._ensure_token()
        balance = self.get_balance()
        return {
            "ok": True,
            "token_prefix": token[:8] + "...",
            "balance": balance,
        }
