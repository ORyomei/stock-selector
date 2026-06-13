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
                "集中度%": f"{h['concentration_pct']}" + ("  ⚠️" if over else ""),
                "損切りまで%": h["dist_to_stop_pct"],
                "利確まで%": h["dist_to_take_pct"],
            })
        st.dataframe(rows, width='stretch', hide_index=True)
        over_list = [h["ticker"] for h in ov["holdings"] if h["concentration_pct"] > ov["max_position_pct"]]
        if over_list:
            st.warning(f"集中超過 (上限{ov['max_position_pct']:.0f}%): {', '.join(over_list)} — 一部利確を検討")
    else:
        st.info("保有銘柄なし")
except Exception as e:
    st.error(f"ポートフォリオ取得エラー: {e}")

st.divider()

# ── パフォーマンス ───────────────────────────────────────────────
st.subheader("📊 パフォーマンス（実現損益）")
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
            import pandas as pd

            df = pd.DataFrame(curve)
            df = df.set_index("date")
            st.line_chart(df, y="cum_pnl", height=240)

        by_reason = perf["by_reason"]
        if by_reason:
            st.caption("クローズ理由別")
            st.dataframe(
                [{"理由": k, "件数": v["count"], "損益¥": f"{v['pnl']:+,.0f}"} for k, v in by_reason.items()],
                width='stretch', hide_index=True,
            )
    else:
        st.info("クローズ済み取引がまだありません")
except Exception as e:
    st.error(f"パフォーマンス取得エラー: {e}")

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

# ── サイクルログ ─────────────────────────────────────────────────
with st.expander("🗒 デーモンログ（直近60行）"):
    try:
        st.code(dd.recent_cycle_log(60), language=None)
    except Exception as e:
        st.error(f"ログ取得エラー: {e}")
