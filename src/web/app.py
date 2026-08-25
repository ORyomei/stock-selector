"""取引状況ダッシュボード (Streamlit, 読み取り専用)。

起動: stock-selector dashboard   (内部で `streamlit run` する)
CLI と同じく共有ファイルを読むだけで、ブローカー/デーモンには触れない。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from web import dashboard_data as dd  # noqa: E402

st.set_page_config(page_title="Stock Selector ダッシュボード", page_icon="📈", layout="wide")


# ── ヘッダー / ステータスバー ─────────────────────────────────────
st.title("📈 Stock Selector — 取引状況")

col_refresh, col_ts = st.columns([1, 4])
with col_refresh:
    if st.button("🔄 更新"):
        st.rerun()
with col_ts:
    st.caption(f"データ取得: {datetime.now():%Y-%m-%d %H:%M:%S} （portfolio.json / diary の保存時点）")

ds = dd.daemon_status()
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("デーモン", "稼働中 🟢" if ds["running"] else "停止 🔴", help=f"PID={ds['pid']}")
c2.metric("ブローカー", ds["broker"])
c3.metric("東証", "場中 🟢" if ds["jp_market_open"] else "時間外 🔴")
c4.metric("AI手仕舞い", "ON" if ds["ai_exit"] else "OFF")
c5.metric("振り返り学習", "ON" if ds["ai_reflection"] else "OFF")
if ds.get("last_cycle"):
    st.caption(f"最新サイクル: {ds['last_cycle']}")

# 障害アラート (watchdog / systemd OnFailure が logs/alerts.log に書く)
try:
    _alerts = dd.recent_alerts(hours=24)
    if _alerts:
        st.error("**直近24時間の障害アラート**\n\n" + "\n\n".join(f"- {a}" for a in _alerts))
except Exception:
    pass

st.divider()

# ── ポートフォリオ ───────────────────────────────────────────────
st.subheader("💼 ポートフォリオ")
try:
    ov = dd.portfolio_overview()
    m1, m2, m3 = st.columns(3)
    m1.metric("総資産 (JPY概算)", f"¥{ov['equity_jpy']:,.0f}")
    m2.metric("現金", f"¥{ov['cash_jpy']:,.0f}" + (f" / ${ov['cash_usd']:,.0f}" if ov['cash_usd'] else ""))
    m3.metric("含み損益", f"¥{ov['unrealized_pnl']:+,.0f}")

    if ov["holdings"]:
        import pandas as pd

        rows = []
        for h in ov["holdings"]:
            over = h["concentration_pct"] > ov["max_position_pct"]
            rows.append({
                "銘柄": h["ticker"],
                "銘柄名": h.get("name", ""),
                "数量": h["qty"],
                "取得": h["entry"],
                "現在": h["current"],
                "損益%": h["pnl_pct"],
                "損益¥": h["pnl"],
                "保有日": h.get("hold_days"),
                "損益%/日": h.get("pnl_pct_per_day"),
                "集中度%": f"{h['concentration_pct']}" + ("  ⚠️" if over else ""),
                "損切りまで%": h["dist_to_stop_pct"],
                "利確まで%": h["dist_to_take_pct"],
            })

        # 日本式カラー: 上昇 = 赤 / 下落 = 緑 (損益セルの文字色)
        def _jp_pnl_color(v: object) -> str:
            if isinstance(v, int | float):
                if v > 0:
                    return "color: #e53935; font-weight: 600"
                if v < 0:
                    return "color: #00a86b; font-weight: 600"
            return ""

        styled = (
            pd.DataFrame(rows)
            .style.map(_jp_pnl_color, subset=["損益%", "損益¥", "損益%/日"])
            .format(
                {"損益%": "{:+.2f}", "損益¥": "{:+,.0f}", "損益%/日": "{:+.2f}"},
                na_rep="—",
            )
        )
        st.dataframe(styled, width='stretch', hide_index=True)
        over_list = [h["ticker"] for h in ov["holdings"] if h["concentration_pct"] > ov["max_position_pct"]]
        if over_list:
            st.warning(f"集中超過 (上限{ov['max_position_pct']:.0f}%): {', '.join(over_list)} — 一部利確を検討")
    else:
        st.info("保有銘柄なし")
except Exception as e:
    st.error(f"ポートフォリオ取得エラー: {e}")

st.divider()

# ── パフォーマンス ───────────────────────────────────────────────
st.subheader("📊 パフォーマンス")


@st.cache_data(ttl=30, show_spinner=False)
def _cycles() -> list:
    """サイクル一覧。グラフのクリック連動と下部の帯で共用する (毎回読み直さない)。"""
    return dd.cycle_index()


def _cycle_at(x: str) -> str | None:
    """時刻 x を含むサイクル名 (x 以前で最も新しいもの)。"""
    prior = [c for c in _cycles() if c["dt"].strftime("%Y-%m-%d %H:%M:%S") <= x]
    if prior:
        return max(prior, key=lambda c: c["dt"])["name"]
    cs = _cycles()
    return min(cs, key=lambda c: c["dt"])["name"] if cs else None

# 総資産(含み込み)と実現損益(確定分)を「元本からの損益¥」に揃えて重ねる。
# 2本の差がそのまま含み損益になり、実現損益だけ見る危険を可視化できる。
try:
    hist = dd.equity_history()
    if len(hist) >= 2:
        from streamlit_echarts import JsCode as _JsCode
        from streamlit_echarts import st_echarts as _st_echarts

        _init = dd.initial_capital_jpy()
        _last = hist[-1]["equity_jpy"]
        _total_pnl = _last - _init
        _curve = dd.performance()["equity_curve"]
        _realized = _curve[-1]["cum_pnl"] if _curve else 0.0

        e1, e2, e3, e4 = st.columns(4)
        e1.metric("総資産", f"¥{_last:,.0f}", delta=f"{(_last / _init - 1) * 100:+.2f}%")
        e2.metric("損益", f"¥{_total_pnl:+,.0f}", help=f"元本 ¥{_init:,.0f} からの増減")
        e3.metric("うち実現", f"¥{_realized:+,.0f}", help="決済して確定した分")
        e4.metric("うち含み", f"¥{_total_pnl - _realized:+,.0f}", help="保有中で未確定の分")

        # 実現損益は決済時にしか点が無いので、総資産の期間の端まで水平に延ばす
        _x0, _x1 = hist[0]["x"], hist[-1]["x"]
        _r_data = [[_x0, 0.0]]
        _r_data += [[c.get("x", c["date"]), c["cum_pnl"]] for c in _curve]
        _r_data += [[_x1, _realized]]

        # 約定マーカー。総資産線の上に、その約定を反映した直後のスナップショット値で打つ
        import bisect

        _xs = [h["x"] for h in hist]
        _ys = [h["equity_jpy"] - _init for h in hist]

        def _y_at(x: str) -> float:
            i = bisect.bisect_left(_xs, x)
            return _ys[min(max(i, 0), len(_ys) - 1)]

        _buy, _sell = [], []
        for m in dd.trade_markers():
            label = f"{m['action']} {m['ticker']}"
            if m.get("quantity"):
                label += f" {m['quantity']}株"
            if m["action"] != "BUY" and m.get("pnl") is not None:
                label += f" / 損益 ¥{m['pnl']:+,.0f}"
            if m.get("source"):
                label += f" ({m['source']})"
            (_buy if m["action"] == "BUY" else _sell).append([m["x"], _y_at(m["x"]), label])

        # 軸ツールチップに統一。scatter は data[2] の説明文、line は金額を出す
        # (series 単位の trigger:'item' は line の axis ツールチップに食われるため)
        _tip = _JsCode(
            "function(ps){var s=ps[0].axisValueLabel;"
            "ps.forEach(function(p){"
            "  if(p.seriesType==='scatter'){s+='<br/>'+p.marker+p.data[2];}"
            "  else{s+='<br/>'+p.marker+p.seriesName+': ¥'"
            "       +Number(p.value[1]).toLocaleString();}"
            "});return s;}"
        ).js_code

        # 指数オーバーレイ: 同じ元本を指数に投じていた場合の損益線
        _idx = dd.index_overlay()

        _yen = _JsCode("function(v){return '¥'+Number(v).toLocaleString();}").js_code
        _clicked = _st_echarts(
            options={
                "tooltip": {"trigger": "axis", "formatter": _tip},
                "legend": {
                    "data": [
                        "総資産（含み込み）", "実現損益（確定分）",
                        "日経平均（同額投資）", "TOPIX（同額投資）", "買い", "決済",
                    ],
                    # TOPIX は既定で非表示 (凡例クリックで表示可)。線が多いと読みにくいため
                    "selected": {"TOPIX（同額投資）": False},
                    "top": 0,
                },
                "grid": {"left": 84, "right": 24, "top": 36, "bottom": 56},
                "xAxis": {"type": "time"},
                "yAxis": {
                    "type": "value", "name": "元本からの損益(¥)", "scale": True,
                    "axisLabel": {"formatter": _yen},
                },
                "dataZoom": [
                    {"type": "inside", "filterMode": "filter"},
                    {"type": "slider", "filterMode": "filter", "height": 20, "bottom": 8},
                ],
                "series": [
                    {
                        "name": "総資産（含み込み）",
                        "type": "line",
                        "showSymbol": False,
                        "areaStyle": {"opacity": 0.12},
                        "data": [[h["x"], h["equity_jpy"] - _init] for h in hist],
                        # 損益0 (=元本) の基準線
                        "markLine": {
                            "silent": True, "symbol": "none",
                            "lineStyle": {"type": "dashed", "color": "#999"},
                            "label": {"formatter": "元本"},
                            "data": [{"yAxis": 0}],
                        },
                    },
                    {
                        "name": "実現損益（確定分）",
                        "type": "line",
                        "step": "end",  # 決済の瞬間に段が付く
                        "showSymbol": True,
                        "lineStyle": {"width": 2},
                        "data": _r_data,
                    },
                    {
                        "name": "日経平均（同額投資）",
                        "type": "line",
                        "showSymbol": False,
                        "lineStyle": {"width": 1.5, "type": "dashed", "color": "#e6a23c"},
                        "itemStyle": {"color": "#e6a23c"},
                        "data": _idx["nikkei"],
                    },
                    {
                        "name": "TOPIX（同額投資）",
                        "type": "line",
                        "showSymbol": False,
                        "lineStyle": {"width": 1.5, "type": "dashed", "color": "#909399"},
                        "itemStyle": {"color": "#909399"},
                        "data": _idx["topix"],
                    },
                    {
                        "name": "買い",
                        "type": "scatter",
                        "symbol": "triangle",
                        "symbolSize": 12,
                        "itemStyle": {"color": "#2e7d32"},
                        "data": _buy,
                    },
                    {
                        "name": "決済",
                        "type": "scatter",
                        "symbol": "diamond",
                        "symbolSize": 13,
                        "itemStyle": {"color": "#d84315"},
                        "data": _sell,
                    },
                ],
            },
            height="340px",
            # クリックした点の [x, y, ...] を Python 側へ返す
            events={"click": "function(p){return p.data;}"},
            key="perf_chart",
        )
        st.caption(
            "2本の差＝含み損益。実サイクル毎のスナップショット（取引時間内のみ記録）。"
            "実現損益は決済した瞬間だけ段が付きます。"
            "**点や約定マーカーをクリックすると、下の AI 思考トレースがその時刻のサイクルに切り替わります**"
        )

        # グラフのクリック → その時刻を含むサイクルを選択して下部へ反映
        if isinstance(_clicked, list) and _clicked and isinstance(_clicked[0], str):
            _name = _cycle_at(_clicked[0])
            if _name and st.session_state.get("sel_cycle") != _name:
                st.session_state["sel_cycle"] = _name
                st.rerun()
    else:
        st.caption("総資産の時系列はサイクル毎に蓄積中 (データが貯まると表示されます)")
except Exception as e:
    st.error(f"総資産グラフ取得エラー: {e}")

with st.expander("実現損益の内訳（確定分のみ・期待値の母数）", expanded=False):
    st.caption(
        "決済（CLOSE）した取引だけを積み上げたもの。買っただけでは損益は確定しないため"
        "点は増えません。勝率・期待値はこの確定分から計算しています。"
    )
    try:
        perf = dd.performance()
        s = perf["stats"]
        if s.get("count"):
            p1, p2, p3, p4 = st.columns(4)
            p1.metric("クローズ数", s["count"])
            p2.metric("勝率", f"{s['win_rate_pct']}%")
            p3.metric("累積実現損益", f"¥{s['total_pnl']:+,.0f}")
            p4.metric("平均勝ち / 負け", f"¥{s['avg_win']:+,.0f} / ¥{s['avg_loss']:+,.0f}")

            curve = perf["equity_curve"]
            if curve:
                from streamlit_echarts import JsCode, st_echarts

                # dataZoom(inside=スクロール/ドラッグ, slider=下部バー)。
                # filterMode='filter' で横ズーム時に範囲外の点を除外 → yAxis(scale=True)
                # が表示範囲の最大・最小に自動再計算される。
                yen_axis = JsCode("function(v){return '¥'+Number(v).toLocaleString();}").js_code
                tooltip_fmt = JsCode(
                    "function(ps){var p=ps[0];"
                    "return p.axisValueLabel+'<br/>累積損益: ¥'"
                    "+Number(p.value[1]).toLocaleString();}"
                ).js_code
                st_echarts(
                    options={
                        "tooltip": {"trigger": "axis", "formatter": tooltip_fmt},
                        "grid": {"left": 72, "right": 24, "top": 24, "bottom": 64},
                        "xAxis": {"type": "time"},
                        "yAxis": {
                            "type": "value",
                            "name": "累積損益(¥)",
                            "scale": True,  # 0 を強制せず表示範囲の min/max にフィット
                            "axisLabel": {"formatter": yen_axis},
                        },
                        "dataZoom": [
                            {"type": "inside", "filterMode": "filter"},
                            {"type": "slider", "filterMode": "filter",
                             "height": 22, "bottom": 12},
                        ],
                        "series": [
                            {
                                "name": "累積実現損益",
                                "type": "line",
                                "step": "end",  # 決済の瞬間に段が付く階段線
                                "showSymbol": True,
                                "areaStyle": {"opacity": 0.12},
                                "data": [[c.get("x", c["date"]), c["cum_pnl"]] for c in curve],
                            }
                        ],
                    },
                    height="260px",
                )

            by_reason = perf["by_reason"]
            if by_reason:
                st.caption("クローズ理由別")
                st.dataframe(
                    [{"理由": k, "件数": v["count"], "損益¥": f"{v['pnl']:+,.0f}"}
                     for k, v in by_reason.items()],
                    width='stretch', hide_index=True,
                )
        else:
            st.info("クローズ済み取引がまだありません")
    except Exception as e:
        st.error(f"パフォーマンス取得エラー: {e}")

st.divider()

# ── ベンチマーク・期待値 ─────────────────────────────────────────
st.subheader("📐 ベンチマーク・期待値")
try:
    bm = dd.benchmark(days=90)
    m1, m2, m3 = st.columns(3)
    m1.metric(
        "TOPIX (1306.T, 90日)",
        f"{bm['topix_pct']:+.1f}%" if bm["topix_pct"] is not None else "—",
    )
    m2.metric(
        "実現損益リターン (90日)",
        f"{bm['system_pct']:+.2f}%" if bm["system_pct"] is not None else "—",
        help="期間内の実現損益 ÷ 初期資金。含み損益は含まない",
    )
    m3.metric(
        "アルファ (対TOPIX)",
        f"{bm['alpha_pct']:+.1f}%" if bm["alpha_pct"] is not None else "—",
        delta=f"{bm['alpha_pct']:+.1f}%" if bm["alpha_pct"] is not None else None,
    )
    st.caption(
        "⚠️ このアルファは「実現損益のみ」対「TOPIXの時価リターン」の比較です。"
        "保有中の含み益は分子に入らないため、ポジションを持っている間は不当に低く出ます。"
        "実態は上のパフォーマンス（総資産）を参照してください。"
    )

    exp = dd.expectancy(days=120)
    eL, eR = st.columns(2)
    with eL:
        st.caption("保有期間別 期待値 (120日)")
        if exp["by_hold"]:
            st.dataframe(
                [
                    {"保有": k, "件数": v["count"], "勝率%": v["win_rate"],
                     "合計¥": f"{v['total_pnl']:+,}", "平均¥": f"{v['avg_pnl']:+,}"}
                    for k, v in exp["by_hold"].items()
                ],
                width="stretch", hide_index=True,
            )
        else:
            st.caption("(データなし)")
    with eR:
        st.caption("エントリースコア別 期待値 (120日)")
        if exp["by_score"]:
            st.dataframe(
                [
                    {"スコア帯": k, "件数": v["count"], "勝率%": v["win_rate"],
                     "合計¥": f"{v['total_pnl']:+,}", "平均¥": f"{v['avg_pnl']:+,}"}
                    for k, v in exp["by_score"].items()
                ],
                width="stretch", hide_index=True,
            )
        else:
            st.caption("(データなし)")
    st.caption("hold_days / score は 2026-07-04 以降の約定から記録 (それ以前は「不明」バケット)")
except Exception as e:
    st.error(f"ベンチマーク/期待値の取得エラー: {e}")

st.divider()

# ── AI インサイト ────────────────────────────────────────────────
st.subheader("🤖 AI インサイト")
try:
    ins = dd.ai_insights()
    cL, cR = st.columns(2)
    with cL:
        st.markdown("**直近の振り返り教訓**")
        if ins["lessons"]:
            st.markdown("\n".join(ins["lessons"]))
        else:
            st.caption("（直近サイクルの教訓ログなし）")
    with cR:
        st.markdown("**AI手仕舞い助言（履歴）**")
        if ins["exit_advisories"]:
            for line in ins["exit_advisories"]:
                st.text(line)
        else:
            st.caption("（直近の手仕舞い助言なし）")
except Exception as e:
    st.error(f"AIインサイト取得エラー: {e}")

# ── 直近シグナル ─────────────────────────────────────────────────
st.subheader("🎯 直近の売買シグナル")
try:
    sigs = dd.recent_signals()
    if sigs:
        st.dataframe(
            [{"銘柄": s["ticker"], "action": s["action"], "score": s["score"],
              "確信度": s["confidence"], "理由": s["reason"]} for s in sigs],
            width='stretch', hide_index=True,
        )
    else:
        st.caption("シグナル履歴なし")
except Exception as e:
    st.error(f"シグナル取得エラー: {e}")

# ── AI 思考トレース ──────────────────────────────────────────────
st.subheader("🧠 AI 思考トレース")
try:
    cycles = _cycles()
    if not cycles:
        st.caption("トレースなし（次の実サイクルで生成されます）")
    else:
        names = {c["name"] for c in cycles}
        if st.session_state.get("sel_cycle") not in names:
            st.session_state["sel_cycle"] = cycles[0]["name"]

        _ICON = {"filled": "🟢", "mixed": "🟢⚠️", "rejected": "⚠️", "none": "⚪"}
        _WD = "月火水木金土日"

        days = list(dict.fromkeys(c["dt"].strftime("%Y-%m-%d") for c in cycles))
        by_day = {
            d: sorted((c for c in cycles if c["dt"].strftime("%Y-%m-%d") == d),
                      key=lambda c: c["dt"])
            for d in days
        }
        # 1日ぶんは折り返さず必ず1行。全日で列数を揃えて時刻の位置を合わせる
        n_cols = max(8, max(len(v) for v in by_day.values()))

        # 列数が増えても潰れないようボタンを詰める (この帯だけに効かせる)
        st.markdown(
            """<style>
            .st-key-cycle_strip div[data-testid="stHorizontalBlock"] { gap: 0.15rem; }
            .st-key-cycle_strip button {
                padding: 0.2rem 0.05rem; min-height: 0; white-space: nowrap;
            }
            .st-key-cycle_strip button p { font-size: 0.92rem; line-height: 1.35; }
            </style>""",
            unsafe_allow_html=True,
        )

        with st.container(key="cycle_strip"):
            for d in days:
                day_asc = by_day[d]
                d0 = day_asc[0]["dt"]
                n_filled = sum(1 for c in day_asc if c["trades"])
                st.markdown(
                    f"**{d0.strftime('%m/%d')}（{_WD[d0.weekday()]}）** "
                    f"<span style='color:#888;font-size:0.85em'>"
                    f"{len(day_asc)}サイクル / 約定 {n_filled}件</span>",
                    unsafe_allow_html=True,
                )
                cols = st.columns(n_cols)
                for col, c in zip(cols, day_asc, strict=False):
                    with col:
                        picked = c["name"] == st.session_state["sel_cycle"]
                        parts = [
                            f"{'✅' if g['filled'] else '✗'} {g['action']} {g['ticker']}"
                            f"(score {g['score']})"
                            for g in c["signals"]
                        ]
                        # シグナル由来でない約定 (自動クローズ・損切り等) も出す
                        sig_tickers = {g["ticker"] for g in c["signals"]}
                        parts += [
                            f"✅ {t['action']} {t['ticker']}"
                            for t in c["trades"] if t["ticker"] not in sig_tickers
                        ]
                        tip = "、".join(parts) or "シグナルなし"
                        if st.button(
                            f"{_ICON[c['status']]}{c['dt'].strftime('%H:%M')}",
                            key=f"cyc_{c['name']}",
                            width='stretch',
                            help=tip,
                            type="primary" if picked else "secondary",
                        ):
                            st.session_state["sel_cycle"] = c["name"]
                            st.rerun()

        st.caption(
            "🟢 約定あり / 🟢⚠️ 約定と未約定が同居 / ⚠️ シグナルは出たが却下 / ⚪ シグナル0件"
            "（ドライランは約定記録が残らないため ⚠️ になります）"
        )

        sel = st.session_state["sel_cycle"]
        cur = next(c for c in cycles if c["name"] == sel)
        detail = "、".join(
            f"{t['action']} {t['ticker']}"
            + (f" {t['quantity']}株" if t.get("quantity") else "")
            for t in cur["trades"]
        )
        st.markdown(
            f"#### {cur['dt'].strftime('%Y-%m-%d %H:%M')}（{_WD[cur['dt'].weekday()]}） "
            f"{cur['market']}　シグナル {len(cur['signals'])}件"
            + (f"　🟢 約定: {detail}" if detail else "")
        )

        # そのサイクルで起きたこと一覧。AI シグナルと、シグナル由来でない約定
        # (損切り・利確・AI手仕舞い等) の両方を並べる
        by_ticker = {t["ticker"]: t for t in cur["trades"]}
        rows = []
        for g in cur["signals"]:
            t = by_ticker.get(g["ticker"]) if g["filled"] else None
            rows.append({
                "結果": "✅ 約定" if g["filled"] else "✗ 未約定",
                "銘柄": g["ticker"] + (f" ← {g['sell_ticker']}" if g.get("sell_ticker") else ""),
                "action": g["action"],
                "数量": t["quantity"] if t else None,
                "score": g["score"],
                "確信度": g["confidence"],
                "損益¥": t["pnl"] if t else None,
                "由来": "AIシグナル",
                "理由": g["reason"],
            })
        sig_tickers = {g["ticker"] for g in cur["signals"]}
        for t in cur["trades"]:
            if t["ticker"] in sig_tickers:
                continue
            rows.append({
                "結果": "✅ 約定",
                "銘柄": t["ticker"],
                "action": t["action"].lower(),
                "数量": t["quantity"],
                "score": None,
                "確信度": None,
                "損益¥": t["pnl"],
                "由来": t["source"] or "—",
                "理由": t["reason"],
            })

        if rows:
            st.dataframe(rows, width='stretch', hide_index=True)
        else:
            st.caption("このサイクルはシグナル0件・約定なし（無理に買わない判断も正当）")

        steps = dd.load_trace(sel)
        if steps:
            st.markdown("**フロー（ツール呼び出しの流れ）**")
            view = st.radio(
                "表示形式", ["シーケンス図", "フロー図"],
                horizontal=True, label_visibility="collapsed", key="flow_view",
            )
            if view == "シーケンス図":
                # st.html は SVG をサニタイズで落とすので iframe 経由で描く
                import streamlit.components.v1 as components

                svg, svg_h = dd.trace_to_sequence_svg(steps)
                components.html(svg, height=min(svg_h + 16, 760), scrolling=True)
                st.caption(
                    "実線=呼び出し / 破線=結果。🤖 紫 = 内部で入れ子 AI が動くツール"
                    "（低速・レート枠を消費）。淡色 = このサイクルで未使用。"
                    "ツール定義を読み込む ToolSearch は除外。引数の全文は下のタイムラインで確認できます。"
                )
            else:
                st.graphviz_chart(dd.trace_to_dot(steps))

            st.markdown("**タイムライン**")
            icon = {"user": "👤", "reasoning": "💭", "tool_call": "🔧",
                    "tool_result": "↩️", "final": "✅"}
            for i, s in enumerate(steps, 1):
                t = s["type"]
                if t == "tool_call":
                    args = s.get("args", {}) or {}
                    arg_s = ", ".join(f"{k}={v}" for k, v in args.items() if k != "signals")
                    head = f"{icon[t]} **{s['tool']}**" + (f"（{arg_s}）" if arg_s else "")
                    with st.expander(f"{i}. {head}", expanded=False):
                        st.json(args)
                elif t == "tool_result":
                    with st.expander(f"{i}. ↩️ {s['tool']} の結果", expanded=False):
                        st.text(s.get("summary", ""))
                elif t == "reasoning":
                    st.markdown(f"{i}. 💭 _{s['text']}_")
                elif t == "final":
                    st.markdown(f"{i}. ✅ **最終判断**")
                    st.info(s["text"])
                elif t == "user":
                    st.markdown(f"{i}. 👤 {s['text']}")
        else:
            st.caption("（トレース読み込み失敗）")
except Exception as e:
    st.error(f"トレース取得エラー: {e}")

# ── サイクルログ ─────────────────────────────────────────────────
with st.expander("🗒 デーモンログ（直近60行）"):
    try:
        st.code(dd.recent_cycle_log(60), language=None)
    except Exception as e:
        st.error(f"ログ取得エラー: {e}")
