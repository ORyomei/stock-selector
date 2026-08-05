#!/usr/bin/env python3
"""ニュース本文まで読み込む詳細リサーチ (ツール内で入れ子 AI を使う唯一のツール)。

メインエージェントは Web アクセスを全面禁止されている
(agents/claude_agent.py の _DISALLOWED_BUILTINS が WebFetch/WebSearch/Bash を遮断)
ため、記事本文に到達する経路はこのモジュールだけ。数万文字の本文を入れ子 LLM で
数百トークンに圧縮して返すので、メインの文脈を圧迫せずに「見出しだけ」を超えられる。

設計上の判断:
- Google News RSS のリンクは JS リダイレクトのシェル (約590KB) しか返さず本文に
  到達できない。本文は yfinance のニュース (発行元 URL が直接得られる) から取得し、
  Google News は見出しの補強にのみ使う
- AI が失敗/タイムアウトしても、取得済みの本文抜粋と見出しを返してツールとしては
  成立させる (ai.used=False で呼び出し側に伝える)
- 入れ子 AI の provider/model/入出力サイズを戻り値に載せる。tool_result は
  トレースに記録されるので、ダッシュボードから入れ子呼び出しを監査できる
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agents.ai import call_ai, parse_ai_json  # noqa: E402

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
_FETCH_TIMEOUT = 8  # 1記事あたりの取得タイムアウト(秒)
_FETCH_WORKERS = 5  # 本文取得の並列数
_BODY_CHARS = 4000  # 1記事あたり AI に渡す本文の上限
_TOTAL_CHARS = 20000  # プロンプトに載せる本文の合計上限
_MIN_BODY = 400  # これ未満はナビ/同意画面とみなし本文として扱わない
_DROP_TAGS = ["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]


def _extract_text(html: str) -> str:
    """HTML から本文らしいテキストを抜き出す。

    <article> / <main> があればそこに絞る。ニュースサイトはグローバルナビや
    関連記事リンクの定型文が長く、そのまま渡すと本文が薄まるため。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup
    return re.sub(r"\s+", " ", root.get_text(" ", strip=True)).strip()


def _fetch_body(url: str) -> str:
    """記事本文を取得する。失敗 (403/404/有料記事/タイムアウト) は空文字。"""
    try:
        r = requests.get(url, headers=_UA, timeout=_FETCH_TIMEOUT)
        if r.status_code != 200:
            return ""
        text = _extract_text(r.text)
        return text[:_BODY_CHARS] if len(text) >= _MIN_BODY else ""
    except Exception:
        return ""


_YF_JP = "https://finance.yahoo.co.jp"


def _jp_news(ticker: str, limit: int) -> list[dict[str, Any]]:
    """日本株の銘柄ニュース一覧を Yahoo!ファイナンスから取得する。

    yfinance が日本株に返すのは米国発の英語記事が中心で、決算・上方修正といった
    売買判断に直結する国内材料が落ちる。日本語ソースを一次に据えるための経路。
    """
    from bs4 import BeautifulSoup

    try:
        r = requests.get(f"{_YF_JP}/quote/{ticker}/news", headers=_UA, timeout=_FETCH_TIMEOUT)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/news/detail/" not in href or href in seen:
            continue
        seen.add(href)
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        out.append({
            "title": title,
            "url": href if href.startswith("http") else _YF_JP + href,
            "published": "",
            "publisher": "Yahoo!ファイナンス",
        })
        if len(out) >= limit:
            break
    return out


def _yf_news(ticker: str, limit: int) -> list[dict[str, Any]]:
    """yfinance のニュース一覧 (発行元 URL 付き) を返す。"""
    import yfinance as yf

    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for it in items[:limit]:
        c = it.get("content", it)
        url = (c.get("canonicalUrl") or {}).get("url") or c.get("link") or ""
        title = c.get("title") or ""
        if not url or not title:
            continue
        out.append({
            "title": title,
            "url": url,
            "published": str(c.get("pubDate") or c.get("providerPublishTime") or ""),
            "publisher": (c.get("provider") or {}).get("displayName") or "",
        })
    return out


def _company_name(ticker: str) -> str:
    try:
        from core.portfolio_ops import get_ticker_name

        return get_ticker_name(ticker) or ticker
    except Exception:
        return ticker


def _headlines(query: str, limit: int) -> list[dict[str, Any]]:
    """Google News の見出し (本文なし)。core.news は無記事時に SystemExit するため直接叩く。"""
    try:
        from infra.container import get_container

        return get_container().news().fetch_headlines(query, lang="ja", limit=limit)
    except Exception:
        return []


def _build_prompt(ticker: str, company: str, sources: list[dict[str, Any]],
                  headlines: list[dict[str, Any]]) -> str:
    parts = [
        f"以下は {company} ({ticker}) に関するニュース記事の本文と見出しです。",
        "投資判断の材料として要点を抽出してください。",
        "",
        "## 記事本文",
    ]
    budget = _TOTAL_CHARS
    for i, s in enumerate(sources, 1):
        body = s["body"][:budget]
        if not body:
            break
        budget -= len(body)
        parts.append(f"\n### {i}. {s['title']} ({s.get('publisher') or '出典不明'})\n{body}")

    if headlines:
        parts.append("\n## 追加の見出し (本文なし)")
        parts += [f"- {h.get('title', '')}" for h in headlines]

    parts += [
        "",
        "## 出力形式",
        "以下の JSON のみを出力してください。記事に書かれていないことは推測しないこと。",
        "根拠が記事にない項目は空配列にしてください。",
        json.dumps({
            "summary": "3〜5文の要約",
            "key_facts": ["記事に明記された事実（数値があれば含める）"],
            "catalysts": ["株価を押し上げうる材料"],
            "risks": ["下振れ材料・懸念"],
            "sentiment": "positive | neutral | negative",
            "confidence": 0.0,
            "sources_used": [1, 2],
        }, ensure_ascii=False, indent=2),
    ]
    return "\n".join(parts)


def run_deep_research(
    ticker: str,
    limit: int = 5,
    provider: str = "claude_code",
    model: str | None = "sonnet",
) -> dict[str, Any]:
    """記事本文を取得し、入れ子 AI で要約して返す。

    Args:
        ticker: ティッカーシンボル (例: 7203.T, NVDA)
        limit: 本文取得を試みる記事数
        provider: 入れ子 AI のプロバイダー
        model: 入れ子 AI のモデル

    Returns:
        research (AI 要約) / sources (取得元) / ai (入れ子呼び出しの記録) を含む dict。
        AI が失敗しても sources と headlines は必ず返す。
    """
    company = _company_name(ticker)
    # 日本株は日本語ソースを一次に。取れなければ yfinance にフォールバック
    if ticker.endswith(".T"):
        candidates = _jp_news(ticker, limit) or _yf_news(ticker, limit)
    else:
        candidates = _yf_news(ticker, limit)
    headlines = _headlines(company, limit)

    sources: list[dict[str, Any]] = []
    if candidates:
        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as ex:
            bodies = list(ex.map(_fetch_body, [c["url"] for c in candidates]))
        for c, body in zip(candidates, bodies, strict=True):
            sources.append({**c, "body": body, "body_chars": len(body)})

    with_body = [s for s in sources if s["body"]]
    result: dict[str, Any] = {
        "ticker": ticker,
        "company": company,
        "articles_found": len(candidates),
        "bodies_fetched": len(with_body),
        "sources": [{k: v for k, v in s.items() if k != "body"} for s in sources],
        "headlines": [h.get("title", "") for h in headlines],
    }

    if not with_body:
        result["research"] = None
        result["ai"] = {"used": False, "reason": "本文を取得できた記事が0件"}
        return result

    prompt = _build_prompt(ticker, company, with_body, headlines)
    ai_meta: dict[str, Any] = {
        "used": False,
        "provider": provider,
        "model": model or "default",
        "prompt_chars": len(prompt),
        "articles_in_prompt": len(with_body),
    }
    try:
        text = call_ai(
            prompt,
            provider,
            model,
            system_msg="ニュース記事から投資判断材料を抽出するAI。JSONのみで回答。",
        )
        research = parse_ai_json(text)
        if research:
            ai_meta["used"] = True
            ai_meta["response_chars"] = len(text or "")
            result["research"] = research
        else:
            ai_meta["error"] = "JSON をパースできませんでした"
            result["research"] = None
    except Exception as e:
        ai_meta["error"] = str(e)
        result["research"] = None

    result["ai"] = ai_meta
    return result
