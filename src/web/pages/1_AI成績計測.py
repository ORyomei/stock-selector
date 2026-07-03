"""AI 有り/無し 成績計測ページ (決定ソース別の実現損益)。"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

SRC_DIR = Path(__file__).resolve().parent.parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from web import dashboard_data as dd  # noqa: E402

st.set_page_config(page_title="AI成績計測", page_icon="🧪", layout="wide")
st.title("🧪 AI 成績計測（決定ソース別の実現損益）")

st.info(
    "各クローズを決定ソース別（AI手仕舞い / 機械ストップ / スワップ / 手動）に分類した実現損益の内訳です。\n\n"
    "⚠️ **これは観測ベースの帰属であり、無作為化A/Bではありません。** AI は弱含み銘柄を早期に"
    "手仕舞う傾向があるため選択バイアスがあり、「AIが優れている/劣っている」と単純比較はできません。"
    "また**エントリーは全てAI（ReActエージェント）**なので、純粋な『AIなしエントリー』の対照群は"
    "ライブデータに存在しません。比較が意味を持つのは主に**手仕舞い側**（AI早期手仕舞い vs 機械ストップ）です。\n\n"
    "source タグは導入以降の約定にのみ付くため、それ以前は「legacy(タグ付け前)」に入ります。"
)

try:
    data = dd.performance_by_source()
    if data["total_closed"] == 0:
        st.warning("クローズ済み取引がまだありません。")
        st.stop()

    st.caption(f"対象クローズ: {data['total_closed']} 件（直近120日）")

    # ── カテゴリ比較 ──
    st.subheader("カテゴリ別比較")
    cat = data["by_category"]
    rows = []
    for name, s in sorted(cat.items(), key=lambda kv: kv[1].get("total_pnl", 0), reverse=True):
        if not s.get("count"):
            continue
        rows.append({
            "ソース": name,
            "件数": s["count"],
            "勝率%": s.get("win_rate"),
            "累積損益¥": f"{s.get('total_pnl', 0):+,.0f}",
            "平均損益¥": f"{s.get('avg_pnl', 0):+,.0f}",
        })
    st.dataframe(rows, width="stretch", hide_index=True)

    # 平均損益の棒グラフ (カテゴリ別)
    import pandas as pd

    chart_df = pd.DataFrame(
        [{"カテゴリ": name, "平均損益": s.get("avg_pnl", 0)}
         for name, s in cat.items() if s.get("count")]
    )
    if not chart_df.empty:
        st.bar_chart(chart_df.set_index("カテゴリ"), y="平均損益", height=240)

    # ── AI手仕舞い vs 機械ストップ の要約 ──
    ai = cat.get("AI手仕舞い", {})
    mech = cat.get("機械ストップ", {})
    if ai.get("count") or mech.get("count"):
        st.subheader("AI手仕舞い vs 機械ストップ")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**🤖 AI手仕舞い (ai_exit / ai_trim)**")
            if ai.get("count"):
                st.metric("件数 / 勝率", f"{ai['count']} 件 / {ai['win_rate']}%")
                st.metric("累積 / 平均損益", f"¥{ai['total_pnl']:+,.0f} / ¥{ai['avg_pnl']:+,.0f}")
            else:
                st.caption("データなし")
        with c2:
            st.markdown("**⚙️ 機械ストップ (mech:*)**")
            if mech.get("count"):
                st.metric("件数 / 勝率", f"{mech['count']} 件 / {mech['win_rate']}%")
                st.metric("累積 / 平均損益", f"¥{mech['total_pnl']:+,.0f} / ¥{mech['avg_pnl']:+,.0f}")
            else:
                st.caption("データなし（機械ストップ未発火 or タグ付け前）")

    # ── 期間A/B (AIフラグ ON/OFF) ──
    st.subheader("📅 期間A/B（AIフラグ ON/OFF 別）")
    st.caption(
        "各クローズをそのクローズ時点のAIフラグ状態に帰属させた実績。"
        "フラグを一定期間OFFにすると ON/OFF の比較が貯まります（時期バイアスは残る）。"
    )
    try:
        period = dd.performance_by_period()
        flag_labels = {
            "ai_exit_advisor": "AI手仕舞い",
            "ai_reflection": "振り返り学習",
            "ai_portfolio_review": "ポートフォリオ推論",
        }
        prows = []
        for flag, label in flag_labels.items():
            d = period["by_flag"].get(flag, {})
            on, off = d.get("ON", {}), d.get("OFF", {})
            prows.append({
                "AI機能": label,
                "ON: 件数": on.get("count", 0),
                "ON: 勝率%": on.get("win_rate", "-"),
                "ON: 平均損益¥": f"{on.get('avg_pnl', 0):+,.0f}" if on.get("count") else "-",
                "OFF: 件数": off.get("count", 0),
                "OFF: 勝率%": off.get("win_rate", "-"),
                "OFF: 平均損益¥": f"{off.get('avg_pnl', 0):+,.0f}" if off.get("count") else "-",
            })
        st.dataframe(prows, width="stretch", hide_index=True)
        if all(r["OFF: 件数"] == 0 for r in prows):
            st.caption("※ 現在すべてのAI機能がONのため OFF 期間の実績はまだありません。"
                       "config の各フラグを一定期間 false にすると比較データが貯まります。")
        flog = period.get("flag_log", [])
        if flog:
            with st.expander("AIフラグ変更履歴"):
                st.dataframe(
                    [{"日時": e.get("ts", "")[:19], **e.get("flags", {})} for e in flog],
                    width="stretch", hide_index=True,
                )
    except Exception as e:
        st.error(f"期間A/B取得エラー: {e}")

    # ── シグナル仮想追跡 (採用 vs 却下の N日後リターン) ──
    st.subheader("🔭 シグナル仮想追跡（採用 vs 却下）")
    st.caption(
        "全シグナル（約定・却下・スキップ）を記録し、シグナル日以降の株価を仮想追跡。"
        "「反証ゲートや上限で却下したシグナルはその後上がったのか」= ゲートが"
        "良いシグナルを殺していないかを検証します（2026-07-04 以降のシグナルから記録）。"
    )
    try:
        ft = dd.signal_followthrough(days=30)
        if not ft["summary"]:
            st.caption("※ シグナル結果ログはまだ蓄積中です。サイクルが回ると貯まります。")
        else:
            frows = []
            for grp, hs in ft["summary"].items():
                row: dict[str, object] = {"グループ": grp}
                for hkey, s in hs.items():
                    row[f"{hkey} 平均%"] = s["avg_pct"]
                    row[f"{hkey} 勝率%"] = s["hit_rate"]
                    row["件数"] = s["count"]
                frows.append(row)
            st.dataframe(frows, width="stretch", hide_index=True)
            with st.expander("個別シグナル (直近50件)"):
                st.dataframe(ft["rows"], width="stretch", hide_index=True)
    except Exception as e:
        st.error(f"シグナル追跡取得エラー: {e}")

    # ── ソース詳細 ──
    with st.expander("ソース詳細（細分）"):
        det = [
            {"source": k, "件数": s["count"], "勝率%": s.get("win_rate"),
             "累積損益¥": f"{s.get('total_pnl', 0):+,.0f}", "平均損益¥": f"{s.get('avg_pnl', 0):+,.0f}"}
            for k, s in sorted(data["by_source"].items(), key=lambda kv: kv[1].get("count", 0), reverse=True)
            if s.get("count")
        ]
        st.dataframe(det, width="stretch", hide_index=True)
except Exception as e:
    st.error(f"計測データ取得エラー: {e}")
