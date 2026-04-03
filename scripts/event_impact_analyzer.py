#!/usr/bin/env python3
"""イベント因果分析スクリプト

マクロ・地政学ニュースを取得し、因果ルールにより
「このニュースがどのセクター・銘柄に影響するか」を予測する。

Usage:
  python3 scripts/event_impact_analyzer.py                         # 最新マクロニュースを自動分析
  python3 scripts/event_impact_analyzer.py --query "トランプ イラン"  # 特定イベントを分析
  python3 scripts/event_impact_analyzer.py --limit 20              # ニュース件数を増やす
  python3 scripts/event_impact_analyzer.py --lang en               # 英語ニュースのみ
"""
import argparse
import json
import re
import sys
import time
from urllib.parse import quote

import feedparser

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 因果ルール知識ベース
#
# 構造:
#   キーワード → 市場方向 → セクター影響 → 代表銘柄
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 影響度: ("positive"|"negative"|"mixed", "high"|"medium"|"low", "理由")
CAUSAL_RULES: dict[str, dict] = {
    "geopolitical_risk": {
        "label": "地政学リスク",
        "keywords": [
            "war", "attack", "military", "missile", "conflict", "sanction",
            "invasion", "iran", "israel", "russia", "ukraine", "north korea",
            "taiwan", "hezbollah", "中東", "戦争", "攻撃", "軍事", "ミサイル",
            "制裁", "紛争", "侵攻", "台湾有事", "北朝鮮", "イラン", "イスラエル",
        ],
        "market_direction": "risk_off",
        "sector_impacts": {
            "defense":     ("positive", "high",   "防衛・軍事支出の増加期待"),
            "oil_energy":  ("positive", "high",   "中東産原油の供給不安"),
            "gold":        ("positive", "high",   "安全資産への資金逃避"),
            "utilities":   ("positive", "low",    "ディフェンシブ銘柄として選好"),
            "airlines":    ("negative", "high",   "燃料コスト急騰・需要消失リスク"),
            "travel":      ("negative", "high",   "リスク回避で旅行・観光が急減"),
            "tech":        ("negative", "medium", "リスクオフでグロース株が売られる"),
            "consumer":    ("negative", "medium", "景況感悪化・消費マインド低下"),
            "auto":        ("negative", "low",    "サプライチェーン混乱時のリスク"),
        },
        "asset_impacts": {
            "oil":    ("positive", "high",   "供給リスクで原油高"),
            "gold":   ("positive", "high",   "安全資産需要で金高"),
            "bonds":  ("positive", "medium", "国債に資金シフト"),
            "usdjpy": ("negative", "medium", "リスクオフで円高"),
            "dxy":    ("negative", "low",    "安全通貨（JPY/CHF）選好でドル安"),
        },
        "us_tickers_positive": ["LMT", "RTX", "NOC", "GD", "HII"],
        "us_tickers_negative": ["AAL", "DAL", "UAL", "CCL", "RCL"],
        "jp_tickers_positive": ["7011.T", "7013.T", "6952.T"],  # 三菱重工・IHI・富士通
        "jp_tickers_negative": ["9201.T", "9202.T", "9726.T"],  # JAL・ANA・HIS
    },

    "rate_hike": {
        "label": "金利引き上げ（タカ派）",
        "keywords": [
            "rate hike", "interest rate hike", "hawkish", "tightening", "fed raises",
            "inflation surge", "CPI high", "利上げ", "金利引き上げ", "インフレ加速",
            "タカ派", "FRB利上げ", "日銀利上げ", "CPIショック",
        ],
        "market_direction": "risk_off",
        "sector_impacts": {
            "banks":        ("positive", "high",   "純利鞘（NIM）拡大で収益改善"),
            "insurance":    ("positive", "medium", "資産運用収益が改善"),
            "real_estate":  ("negative", "high",   "住宅ローン金利上昇・需要萎縮"),
            "tech_growth":  ("negative", "high",   "DCF評価の割引率上昇でバリュエーション低下"),
            "utilities":    ("negative", "medium", "高配当の相対魅力が低下"),
            "consumer":     ("negative", "medium", "ローン負担増で消費が抑制"),
            "bonds":        ("negative", "high",   "価格と金利は逆相関"),
        },
        "asset_impacts": {
            "bonds":  ("negative", "high",   "金利上昇で債券価格下落"),
            "gold":   ("negative", "medium", "実質金利上昇で金の保有コスト増"),
            "usdjpy": ("positive", "medium", "日米金利差拡大でドル高円安"),
            "dxy":    ("positive", "high",   "米金利上昇でドル高"),
        },
        "us_tickers_positive": ["JPM", "BAC", "GS", "MS", "WFC", "C"],
        "us_tickers_negative": ["TSLA", "AMZN", "SNOW", "PLTR", "NET"],
        "jp_tickers_positive": ["8306.T", "8316.T", "8411.T"],  # 三菱UFJ・三井住友・みずほ
        "jp_tickers_negative": ["9984.T", "3436.T", "8802.T"],  # ソフトバンクG・SUMCO・三菱地所
    },

    "rate_cut": {
        "label": "金利引き下げ（ハト派）",
        "keywords": [
            "rate cut", "dovish", "easing", "fed cuts", "stimulus", "QE", "lower rates",
            "利下げ", "金融緩和", "量的緩和", "ハト派", "FRB利下げ", "景気刺激",
        ],
        "market_direction": "risk_on",
        "sector_impacts": {
            "tech_growth":  ("positive", "high",   "割引率低下でグロース株DCF評価が上昇"),
            "real_estate":  ("positive", "high",   "ローン金利低下・住宅・REIT需要増"),
            "consumer":     ("positive", "medium", "ローン負担軽減で消費が回復"),
            "small_cap":    ("positive", "medium", "短期金利に敏感な中小企業が恩恵"),
            "banks":        ("negative", "medium", "純利鞘縮小で収益悪化懸念"),
            "insurance":    ("negative", "low",    "運用収益の悪化"),
        },
        "asset_impacts": {
            "bonds":  ("positive", "high",   "金利低下で債券価格上昇"),
            "gold":   ("positive", "medium", "実質金利低下で金が魅力的に"),
            "usdjpy": ("negative", "medium", "日米金利差縮小で円高"),
            "dxy":    ("negative", "high",   "米金利低下でドル安"),
        },
        "us_tickers_positive": ["TSLA", "NVDA", "AMZN", "MSFT", "GOOG", "META"],
        "us_tickers_negative": ["JPM", "BAC", "GS"],
        "jp_tickers_positive": ["3436.T", "8952.T", "9984.T"],  # SUMCO・ジャパンリアルエステイト
        "jp_tickers_negative": ["8306.T", "8316.T"],
    },

    "ai_tech_boom": {
        "label": "AI・半導体ブーム",
        "keywords": [
            "AI", "artificial intelligence", "ChatGPT", "LLM", "GPU", "semiconductor",
            "nvidia", "data center", "generative AI", "AGI", "foundation model",
            "人工知能", "生成AI", "半導体", "データセンター", "GPU需要", "エヌビディア",
        ],
        "market_direction": "risk_on",
        "sector_impacts": {
            "semiconductors": ("positive", "high",   "GPU・先端チップ需要が爆増"),
            "cloud":          ("positive", "high",   "AI投資でクラウド・インフラ需要拡大"),
            "software":       ("positive", "medium", "AI SaaS企業の成長が加速"),
            "data_center":    ("positive", "high",   "電力・冷却設備への投資増"),
            "utilities":      ("positive", "medium", "データセンター向け電力需要増"),
            "traditional_sw": ("negative", "low",    "AI代替リスクで旧来SWが圧迫"),
        },
        "asset_impacts": {
            "usdjpy": ("positive", "low", "テック株買いでリスクオン・ドル高"),
            "dxy":    ("positive", "low", "米国テック優位でドル高"),
        },
        "us_tickers_positive": ["NVDA", "AMD", "MSFT", "GOOG", "META", "SMCI", "AVGO", "TSM"],
        "us_tickers_negative": ["INTC", "IBM"],
        "jp_tickers_positive": ["6857.T", "6920.T", "4063.T"],  # アドバンテスト・レーザーテック・信越化学
        "jp_tickers_negative": [],
    },

    "recession_fear": {
        "label": "景気後退懸念",
        "keywords": [
            "recession", "GDP decline", "unemployment rise", "layoff", "slowdown",
            "jobless", "economic contraction", "yield inversion",
            "景気後退", "リセッション", "失業増加", "レイオフ", "景気減速",
            "GDP下落", "逆イールド", "消費失速",
        ],
        "market_direction": "risk_off",
        "sector_impacts": {
            "gold":        ("positive", "high",   "安全資産への逃避"),
            "defensives":  ("positive", "medium", "食料・医薬品など生活必需品が底堅い"),
            "consumer":    ("negative", "high",   "消費支出の大幅減少"),
            "industrials": ("negative", "high",   "設備投資が急速に縮小"),
            "financials":  ("negative", "medium", "不良債権・貸し倒れリスク増加"),
            "tech":        ("negative", "medium", "IT支出削減・広告費カット"),
        },
        "asset_impacts": {
            "bonds":  ("positive", "high",   "景気後退ヘッジで国債買い"),
            "gold":   ("positive", "high",   "安全資産需要"),
            "oil":    ("negative", "high",   "需要減退で原油安"),
            "usdjpy": ("negative", "medium", "リスクオフで円高"),
        },
        "us_tickers_positive": ["TLT", "XLP", "WMT", "KO", "PG", "JNJ"],
        "us_tickers_negative": ["CAT", "GE", "F", "GM", "BA"],
        "jp_tickers_positive": ["2914.T", "4519.T"],  # 日本たばこ・中外製薬
        "jp_tickers_negative": ["7203.T", "6752.T", "9201.T"],  # トヨタ・パナソニック・JAL
    },

    "tariff_trade_war": {
        "label": "関税・貿易摩擦",
        "keywords": [
            "tariff", "trade war", "import duty", "export ban", "trade restriction",
            "protectionism", "decoupling", "supply chain",
            "関税", "貿易戦争", "輸入関税", "輸出規制", "貿易摩擦",
            "デカップリング", "サプライチェーン", "トランプ関税",
        ],
        "market_direction": "risk_off",
        "sector_impacts": {
            "domestic_consumer": ("positive", "low",    "輸入品より国内品志向に"),
            "auto_export":       ("negative", "high",   "輸出依存の自動車が打撃"),
            "semiconductors":    ("negative", "high",   "輸出規制・調達困難"),
            "retail":            ("negative", "medium", "輸入コスト増が消費者に転嫁"),
            "industrials":       ("negative", "medium", "部品・原材料の調達コスト増"),
        },
        "asset_impacts": {
            "usdjpy": ("negative", "medium", "リスクオフで円高"),
            "dxy":    ("negative", "low",    "貿易縮小でドル需要減"),
            "gold":   ("positive", "medium", "不確実性ヘッジ"),
        },
        "us_tickers_positive": [],
        "us_tickers_negative": ["AAPL", "NVDA", "QCOM", "MU"],
        "jp_tickers_positive": [],
        "jp_tickers_negative": ["7203.T", "6758.T", "6981.T"],  # トヨタ・ソニー・村田製作所
    },

    "yen_move": {
        "label": "円相場大変動",
        "keywords": [
            "yen weakens", "yen strengthens", "dollar yen", "usdjpy", "boj intervene",
            "円安", "円高", "ドル円", "円相場", "日銀介入", "為替介入",
        ],
        "market_direction": "mixed",
        "sector_impacts": {
            "exporters":     ("positive", "high",   "円安は輸出企業の円換算収益を押し上げ"),
            "tourism_inbound": ("positive", "medium", "円安で訪日外国人が増加"),
            "importers":     ("negative", "high",   "輸入コスト増加・エネルギー・食料に直撃"),
            "retailers":     ("negative", "medium", "輸入品値上がりで販売が鈍化"),
        },
        "asset_impacts": {
            "usdjpy": ("positive", "high", "円安進行"),
            "nikkei": ("positive", "medium", "輸出企業比率の高い日経平均に追い風"),
        },
        "us_tickers_positive": [],
        "us_tickers_negative": [],
        "jp_tickers_positive": ["7203.T", "6758.T", "6902.T", "7751.T"],  # 輸出大手
        "jp_tickers_negative": ["9101.T", "9107.T", "3382.T"],  # 航空・輸送・セブン＆アイ
    },

    "energy_shock": {
        "label": "エネルギー価格急変",
        "keywords": [
            "oil price surge", "crude oil spike", "energy crisis", "OPEC cut",
            "oil embargo", "natural gas",
            "原油高", "原油急騰", "エネルギー危機", "OPEC減産", "天然ガス高騰",
        ],
        "market_direction": "risk_off",
        "sector_impacts": {
            "oil_energy":   ("positive", "high",   "資源価格の上昇で収益急増"),
            "utilities":    ("negative", "high",   "燃料費増で電力・ガス会社の負担増"),
            "airlines":     ("negative", "high",   "燃料費が営業コストの大半を占める"),
            "chemical":     ("negative", "medium", "石化原料コスト増"),
            "transport":    ("negative", "medium", "輸送コスト増加"),
        },
        "asset_impacts": {
            "oil":    ("positive", "high",   "エネルギー価格の直接上昇"),
            "gold":   ("positive", "low",    "インフレヘッジ"),
            "bonds":  ("negative", "medium", "インフレ懸念で債券売り"),
            "usdjpy": ("positive", "low",    "資源国通貨高でドル高相場も"),
        },
        "us_tickers_positive": ["XOM", "CVX", "COP", "EOG", "SLB"],
        "us_tickers_negative": ["AAL", "DAL", "UAL", "FDX", "UPS"],
        "jp_tickers_positive": ["5020.T", "1605.T"],  # ENEOSホールディングス・石油資源開発
        "jp_tickers_negative": ["9201.T", "9202.T"],  # JAL・ANA
    },
}

# マクロニュース収集クエリ（自動モード時に使用）
AUTO_QUERIES = [
    ("en", "geopolitical war attack military conflict sanctions 2026"),
    ("en", "fed interest rate inflation CPI monetary policy 2026"),
    ("en", "tariff trade war export ban supply chain 2026"),
    ("en", "nvidia AI semiconductor data center 2026"),
    ("ja", "中東 戦争 地政学リスク 2026"),
    ("ja", "FRB 利上げ 利下げ 金利 インフレ 2026"),
    ("ja", "関税 貿易摩擦 輸出規制 2026"),
    ("ja", "景気後退 リセッション GDP"),
]


def fetch_news(query: str, lang: str = "en", limit: int = 5, retries: int = 2) -> list[dict]:
    """Google News RSS でニュースを取得する"""
    encoded = quote(query)
    if lang == "ja":
        url = f"https://news.google.com/rss/search?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
    else:
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en&gl=US&ceid=US:en"

    for attempt in range(retries + 1):
        try:
            feed = feedparser.parse(url)
            if feed.entries:
                break
        except Exception as e:
            print(f"  WARN: RSS取得失敗 (attempt {attempt + 1}): {e}", file=sys.stderr)
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    else:
        return []

    articles = []
    for entry in feed.entries[:limit]:
        articles.append({
            "title": entry.get("title", ""),
            "published": entry.get("published", ""),
            "source": entry.get("source", {}).get("title", "") if hasattr(entry.get("source"), "get") else "",
            "link": entry.get("link", ""),
        })
    return articles


def classify_article(title: str) -> list[str]:
    """記事タイトルが該当する因果ルールのキーを全て返す"""
    title_lower = title.lower()
    matched = []
    for rule_key, rule in CAUSAL_RULES.items():
        for kw in rule["keywords"]:
            if kw.lower() in title_lower:
                matched.append(rule_key)
                break
    return matched


def analyze_impacts(articles: list[dict]) -> dict:
    """
    記事リストから因果影響をまとめて返す。

    Returns:
        {
            "triggered_rules": { rule_key: { "label", "articles", "count" } },
            "sector_impacts":  { sector: { "direction", "magnitude", "reason", "rules" } },
            "asset_impacts":   { asset:  { "direction", "magnitude", "reason" } },
            "tickers_to_watch": { "positive": [...], "negative": [...] },
            "market_direction": "risk_off" | "risk_on" | "mixed" | "neutral",
            "event_count": int,
            "top_headlines": [str, ...],
        }
    """
    triggered: dict[str, list[str]] = {}  # rule_key -> [headline, ...]

    for article in articles:
        title = article.get("title", "")
        for rule_key in classify_article(title):
            triggered.setdefault(rule_key, []).append(title)

    if not triggered:
        return {
            "triggered_rules": {},
            "sector_impacts": {},
            "asset_impacts": {},
            "tickers_to_watch": {"positive": [], "negative": []},
            "market_direction": "neutral",
            "event_count": 0,
            "top_headlines": [a["title"] for a in articles[:5]],
        }

    # セクター・資産への影響を集計（同セクターで複数ルールが衝突する場合は強い方を採用）
    sector_map: dict[str, dict] = {}
    asset_map: dict[str, dict] = {}
    direction_votes = {"risk_off": 0, "risk_on": 0, "mixed": 0}
    mag_order = {"high": 3, "medium": 2, "low": 1}

    pos_tickers: set[str] = set()
    neg_tickers: set[str] = set()

    for rule_key, headlines in triggered.items():
        rule = CAUSAL_RULES[rule_key]

        # 市場方向に投票
        md = rule.get("market_direction", "mixed")
        direction_votes[md] = direction_votes.get(md, 0) + len(headlines)

        # セクター影響
        for sector, (direction, magnitude, reason) in rule["sector_impacts"].items():
            existing = sector_map.get(sector)
            if not existing or mag_order[magnitude] > mag_order[existing["magnitude"]]:
                sector_map[sector] = {
                    "direction": direction,
                    "magnitude": magnitude,
                    "reason": reason,
                    "source_rules": [rule["label"]],
                }
            elif existing:
                existing["source_rules"].append(rule["label"])

        # 資産影響
        for asset, (direction, magnitude, *extra) in rule["asset_impacts"].items():
            reason = extra[0] if extra else ""
            existing = asset_map.get(asset)
            if not existing or mag_order[magnitude] > mag_order[existing["magnitude"]]:
                asset_map[asset] = {"direction": direction, "magnitude": magnitude, "reason": reason}

        # 銘柄
        pos_tickers.update(rule.get("us_tickers_positive", []))
        pos_tickers.update(rule.get("jp_tickers_positive", []))
        neg_tickers.update(rule.get("us_tickers_negative", []))
        neg_tickers.update(rule.get("jp_tickers_negative", []))

    # ポジ/ネガ両方にある銘柄は除外（ルールが衝突）
    ambiguous = pos_tickers & neg_tickers
    pos_tickers -= ambiguous
    neg_tickers -= ambiguous

    # 総合市場方向を決定
    overall_direction = max(direction_votes, key=direction_votes.get)

    # triggered_rules を整理
    triggered_output = {}
    for rule_key, headlines in triggered.items():
        triggered_output[rule_key] = {
            "label": CAUSAL_RULES[rule_key]["label"],
            "count": len(headlines),
            "headlines": headlines[:3],
        }

    return {
        "triggered_rules": triggered_output,
        "sector_impacts": sector_map,
        "asset_impacts": asset_map,
        "tickers_to_watch": {
            "positive": sorted(pos_tickers),
            "negative": sorted(neg_tickers),
        },
        "market_direction": overall_direction,
        "event_count": sum(len(v) for v in triggered.values()),
        "top_headlines": [a["title"] for a in articles[:5]],
    }


def format_causal_summary(result: dict) -> str:
    """因果分析結果を人間が読みやすいMarkdownにフォーマット"""
    lines = ["## 📡 イベント因果分析", ""]

    # 市場方向
    dir_map = {"risk_off": "⚠️ リスクオフ", "risk_on": "✅ リスクオン", "mixed": "⚡ 方向性まちまち", "neutral": "➖ 中立"}
    lines.append(f"**総合市場方向: {dir_map.get(result['market_direction'], result['market_direction'])}**")
    lines.append("")

    # トリガーされたルール
    if result["triggered_rules"]:
        lines.append("### 🔑 検知されたマクロイベント")
        for key, info in result["triggered_rules"].items():
            lines.append(f"- **{info['label']}** ({info['count']}件)")
            for h in info["headlines"][:2]:
                lines.append(f"  - {h}")
        lines.append("")

    # セクター影響
    if result["sector_impacts"]:
        lines.append("### 📊 セクター別予測影響")
        pos_sectors = {k: v for k, v in result["sector_impacts"].items() if v["direction"] == "positive"}
        neg_sectors = {k: v for k, v in result["sector_impacts"].items() if v["direction"] == "negative"}

        if pos_sectors:
            lines.append("**↑ 上昇期待:**")
            for sector, info in sorted(pos_sectors.items(), key=lambda x: {"high":3,"medium":2,"low":1}[x[1]["magnitude"]], reverse=True):
                mag_label = {"high": "◎", "medium": "○", "low": "△"}[info["magnitude"]]
                lines.append(f"  - {mag_label} {sector}: {info['reason']}")

        if neg_sectors:
            lines.append("**↓ 下落懸念:**")
            for sector, info in sorted(neg_sectors.items(), key=lambda x: {"high":3,"medium":2,"low":1}[x[1]["magnitude"]], reverse=True):
                mag_label = {"high": "◎", "medium": "○", "low": "△"}[info["magnitude"]]
                lines.append(f"  - {mag_label} {sector}: {info['reason']}")
        lines.append("")

    # 資産影響
    if result["asset_impacts"]:
        lines.append("### 💱 資産・為替への影響")
        asset_labels = {
            "oil": "原油", "gold": "金", "bonds": "国債", "usdjpy": "ドル円",
            "dxy": "ドルインデックス", "nikkei": "日経平均",
        }
        for asset, info in result["asset_impacts"].items():
            arrow = "↑" if info["direction"] == "positive" else "↓"
            label = asset_labels.get(asset, asset)
            lines.append(f"- {label}: {arrow} ({info['magnitude']}) — {info['reason']}")
        lines.append("")

    # 注目銘柄
    if result["tickers_to_watch"]["positive"] or result["tickers_to_watch"]["negative"]:
        lines.append("### 🎯 注目銘柄")
        if result["tickers_to_watch"]["positive"]:
            lines.append(f"- **買い候補**: {', '.join(result['tickers_to_watch']['positive'])}")
        if result["tickers_to_watch"]["negative"]:
            lines.append(f"- **売り・回避**: {', '.join(result['tickers_to_watch']['negative'])}")

    return "\n".join(lines)


def run(query: str | None = None, lang: str | None = None, limit: int = 8) -> dict:
    """
    メイン処理。ニュースを取得して因果分析を返す。

    Args:
        query: 特定のクエリ（None の場合は AUTO_QUERIES を使用）
        lang: 言語指定 (None の場合は AUTO_QUERIES に従う)
        limit: 1クエリあたりの最大取得件数

    Returns:
        analyze_impacts() の結果辞書
    """
    all_articles: list[dict] = []
    seen_titles: set[str] = set()

    if query:
        # 特定クエリモード
        queries = [(lang or "ja", query), ("en", query)]
    else:
        # 自動マクロニュースモード
        queries = AUTO_QUERIES

    for q_lang, q_text in queries:
        articles = fetch_news(q_text, lang=q_lang, limit=limit)
        for a in articles:
            title = a.get("title", "")
            if title and title not in seen_titles:
                seen_titles.add(title)
                all_articles.append(a)
        time.sleep(0.3)  # レート制限緩和

    result = analyze_impacts(all_articles)
    result["total_articles_fetched"] = len(all_articles)
    result["queries_used"] = [q[1] for q in queries]

    return result


def main():
    parser = argparse.ArgumentParser(
        description="イベント因果分析 — ニュースから株価への波及を予測",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scripts/event_impact_analyzer.py
  python3 scripts/event_impact_analyzer.py --query "トランプ イラン 攻撃"
  python3 scripts/event_impact_analyzer.py --query "fed rate cut" --lang en
  python3 scripts/event_impact_analyzer.py --limit 20
        """,
    )
    parser.add_argument("--query", type=str, default=None, help="分析する特定のイベント・クエリ")
    parser.add_argument("--lang", type=str, default=None, choices=["ja", "en"], help="ニュース言語")
    parser.add_argument("--limit", type=int, default=8, help="1クエリあたりの最大取得件数")
    parser.add_argument("--format", choices=["json", "text"], default="json", help="出力形式")
    args = parser.parse_args()

    result = run(query=args.query, lang=args.lang, limit=args.limit)

    if args.format == "text":
        print(format_causal_summary(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
