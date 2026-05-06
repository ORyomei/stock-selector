#!/usr/bin/env python3
"""kabuステーション API 接続テスト

Usage:
  python3 src/scripts/kabu_check.py              # 接続テスト + 残高表示
  python3 src/scripts/kabu_check.py --positions  # 保有ポジション表示
  python3 src/scripts/kabu_check.py --orders     # 未約定注文表示

事前準備:
  1. kabuステーションを起動・ログイン
  2. [ツール] → [API設定] で API を有効化
  3. 環境変数 KABU_API_PASSWORD に API パスワードを設定
  4. config/trading_config.json の "kabu" セクションを必要に応じて調整
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.container import get_container
from trading.brokers import KabuStationBroker
from trading.brokers.kabu_station import KabuStationError


def main() -> int:
    parser = argparse.ArgumentParser(description="kabuステーション API 接続テスト")
    parser.add_argument("--positions", action="store_true", help="保有ポジション表示")
    parser.add_argument("--orders", action="store_true", help="未約定注文表示")
    args = parser.parse_args()

    config = get_container().config_repo().load_trading_config()
    kabu_config = config.get("kabu")
    if not kabu_config:
        print("❌ trading_config.json に kabu セクションがありません", file=sys.stderr)
        return 1

    try:
        broker = KabuStationBroker(kabu_config)
        result = broker.ping()
        print("=" * 60)
        print(
            f"✅ kabu API 接続成功  "
            f"(host={kabu_config.get('host')}:{kabu_config.get('port')}, "
            f"sandbox={kabu_config.get('sandbox')})"
        )
        print(f"  Token: {result['token_prefix']}")
        print(f"  買付余力: ¥{result['balance']['cash_jpy']:,.0f}")
        print("=" * 60)

        if args.positions:
            positions = broker.get_positions()
            print(f"\n📊 保有ポジション: {len(positions)} 件")
            for p in positions:
                print(json.dumps(p.to_dict(), ensure_ascii=False, indent=2))

        if args.orders:
            orders = broker.get_orders()
            print(f"\n📋 未約定注文: {len(orders)} 件")
            for o in orders:
                print(json.dumps(o.to_dict(), ensure_ascii=False, indent=2))

        return 0
    except KabuStationError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        print(
            "\nヒント:\n"
            "  - kabuステーションが起動しログイン済みか確認\n"
            "  - [ツール]→[API設定] で API が有効か確認\n"
            "  - 環境変数 KABU_API_PASSWORD が正しく設定されているか確認\n"
            "  - sandbox=true の場合は検証用ポート 18081、本番は 18080",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
