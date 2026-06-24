"""ブローカー生成ファクトリ。

trading_config.json の "broker" 設定に応じて具象ブローカーを構築する。
唯一のブローカー生成経路 (Strategy + Factory)。container.broker() から
シングルトンとして呼ばれ、プロセス内で1インスタンスを共有する。
"""

from __future__ import annotations

from interfaces.broker import BrokerInterface
from interfaces.repositories.config import ConfigRepository
from interfaces.repositories.portfolio import PortfolioRepository

from .kabu_station import KabuStationBroker
from .simulator import BrokerSimulator


def create_broker(
    config_repo: ConfigRepository,
    portfolio_repo: PortfolioRepository,
) -> BrokerInterface:
    """trading_config.json に応じてブローカーを生成・復元する。

    - "simulator" (default): repo を注入した BrokerSimulator (自己永続化)
    - "kabu": KabuStationBroker (証券会社が状態を所有)
    """
    config = config_repo.load_trading_config()
    broker_name = (config.get("broker") or "simulator").lower()

    if broker_name == "kabu":
        broker = KabuStationBroker(config["kabu"])
        broker.sync()  # 接続確認 + 初期同期
        print(f"✅ kabuステーション API 接続成功 (sandbox={config['kabu'].get('sandbox', False)})")
        return broker

    # default: simulator — repo を注入し、ミューテーション毎に自己永続化させる
    broker = BrokerSimulator(config["simulator"], repo=portfolio_repo)
    # load() はファイルが無い時のみ None を返す。balance だけ持つ「全現金・0ポジション」
    # 状態も正当な保存状態なので復元する (positions が空 [] でも初期化リセットしない)。
    portfolio_data = portfolio_repo.load()
    if portfolio_data and (
        portfolio_data.get("balance")
        or portfolio_data.get("positions")
        or portfolio_data.get("holdings")
    ):
        broker.from_dict(portfolio_data)
        print("✅ ポートフォリオ復元")
    else:
        print("⚠️  ポートフォリオファイルなし - 初期状態で開始")
    return broker
