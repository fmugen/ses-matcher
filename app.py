"""
app.py - SES案件管理 Streamlit UI

起動方法:
    uv run streamlit run app.py
"""
import json
import io
import contextlib
from datetime import datetime

import streamlit as st
import db
from mail_composer import generate_application_mail
from fetch_mails import run as fetch_run

# ──────────────────────────────────────
# ページ設定
# ──────────────────────────────────────
st.set_page_config(
    page_title="SES案件マッチャー",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("🎯 SES案件マッチャー")
st.caption("いろは計画 | Y.O. 案件管理・応募支援システム")

# ──────────────────────────────────────
# サイドバー：フィルター & バッチ実行
# ──────────────────────────────────────
with st.sidebar:
    st.header("🔍 フィルター")

    show_recommend_only = st.checkbox("推奨案件のみ表示", value=True)
    status_options = ["all", "new", "applied", "interview", "rejected", "closed"]
    status_filter = st.selectbox(
        "ステータス",
        status_options,
        format_func=lambda x: {
            "all": "すべて", "new": "未対応", "applied": "応募済",
            "interview": "面談中", "rejected": "不採用", "closed": "終了",
        }.get(x, x),
    )

    st.divider()
    st.header("🔄 メール取込")
    days = st.number_input("取込日数", min_value=1, max_value=90, value=7)
    if st.button("今すぐ取込実行", type="primary", use_container_width=True):
        log_area = st.empty()
        with st.spinner("メールを取込中... (Thunderbird起動中であること)"):
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    new_count, skip_count, error_count = fetch_run(days)
                log_area.text(buf.getvalue()[-1500:])
                st.success(f"取込完了！ 新規:{new_count} スキップ:{skip_count} エラー:{error_count}")
                st.rerun()
            except Exception as e:
                log_area.text(buf.getvalue()[-1500:])
                st.error(f"エラー: {e}")

# ──────────────────────────────────────
# 案件一覧
# ──────────────────────────────────────
jobs = db.get_jobs(
    status_filter=None if status_filter == "all" else status_filter,
    recommend_only=show_recommend_only,
)

# サマリー
col1, col2, col3, col4 = st.columns(4)
all_jobs = db.get_jobs()
col1.metric("総案件数", len(all_jobs))
col2.metric("推奨案件", sum(1 for j in all_jobs if j["recommend"]))
col3.metric("未対応", sum(1 for j in all_jobs if j["status"] == "new"))
col4.metric("応募済", sum(1 for j in all_jobs if j["status"] == "applied"))

st.divider()

if not jobs:
    st.info("表示できる案件がありません。メールを取込んでください。")
    st.stop()

# 一覧テーブル
st.subheader(f"案件一覧（{len(jobs)}件）")

for job in jobs:
    # カードのヘッダー色
    rec = job["recommend"]
    block = job.get("block_reason")
    score = job.get("match_score", 0)

    if rec:
        badge = f"✅ 推奨 (スコア:{score})"
        border_color = "#1D9E75"
    elif block:
        badge = f"❌ NG: {block}"
        border_color = "#E24B4A"
    else:
        badge = f"△ 要検討 (スコア:{score})"
        border_color = "#BA7517"

    with st.container():
        st.markdown(
            f'<div style="border-left: 4px solid {border_color}; padding-left: 12px; margin-bottom: 4px;">',
            unsafe_allow_html=True,
        )

        col_title, col_badge, col_btn = st.columns([5, 2, 1])
        with col_title:
            title = job.get("job_name") or job.get("subject", "（案件名不明）")
            received = job.get("received_at", "")
            date_str = received[:10] if received else ""
            st.markdown(
                f"**{title[:60]}**"
                f"<span style='font-size:12px;color:gray;font-weight:normal;"
                f"margin-left:8px;'>{date_str}</span>",
                unsafe_allow_html=True,
            )
            meta_parts = []
            if job.get("sender_name"):
                meta_parts.append(f"✉️{job['sender_name']}")
            if job.get("location"):
                meta_parts.append(f"📍{job['location']}")
            if job.get("remote_type"):
                meta_parts.append(f"🏠{job['remote_type']}")
            if job.get("unit_price_min"):
                p_max = f"〜{job['unit_price_max']}万" if job.get("unit_price_max") else ""
                meta_parts.append(f"💰{job['unit_price_min']}{p_max}万")
            if job.get("min_years_req"):
                meta_parts.append(f"⏱必須{job['min_years_req']}年")
            if job.get("contract_type"):
                meta_parts.append(f"📄{job['contract_type']}")
            st.caption("　".join(meta_parts))
        with col_badge:
            st.caption(badge)
        with col_btn:
            if st.button("詳細", key=f"detail_{job['id']}"):
                st.session_state["selected_job_id"] = job["id"]
                st.rerun()

        st.markdown("</div>", unsafe_allow_html=True)
        st.write("")

# ──────────────────────────────────────
# 案件詳細 + 応募メール生成
# ──────────────────────────────────────
selected_id = st.session_state.get("selected_job_id")
if selected_id:
    job = db.get_job_by_id(selected_id)
    if job:
        st.divider()
        st.subheader(f"📋 案件詳細: {job.get('job_name') or job.get('subject')}")

        tab1, tab2, tab3 = st.tabs(["案件情報", "元メール", "応募メール生成"])

        with tab1:
            c1, c2 = st.columns(2)
            with c1:
                st.write(f"**発注元:** {job.get('client_company') or '不明'}")
                st.write(f"**勤務地:** {job.get('location') or '不明'}")
                st.write(f"**リモート:** {job.get('remote_type') or '不明'}")
                st.write(f"**開始時期:** {job.get('start_date') or '不明'}")
                st.write(f"**単価:** {job.get('unit_price_min') or '?'}〜{job.get('unit_price_max') or '?'}万円")
                st.write(f"**契約形態:** {job.get('contract_type') or '不明'}")
                st.write(f"**必須年数:** {job.get('min_years_req') or '不明'}年")
                st.write(f"**年齢制限:** {job.get('age_restriction') or 'なし'}")
            with c2:
                req = json.loads(job.get("required_skills") or "[]")
                pref = json.loads(job.get("preferred_skills") or "[]")
                st.write(f"**必須スキル:** {', '.join(req) or '不明'}")
                st.write(f"**尚可スキル:** {', '.join(pref) or 'なし'}")
                st.write(f"**備考:** {job.get('notes') or 'なし'}")
                st.write(f"**マッチスコア:** {job.get('match_score') or 0}/100")
                if job.get("block_reason"):
                    st.error(f"NG理由: {job['block_reason']}")
                st.write(f"**送信元:** {job.get('sender_name')} &lt;{job.get('sender_email')}&gt;")

            st.divider()
            st.write("**営業ステータス管理**")
            col_s1, col_s2 = st.columns([2, 3])
            with col_s1:
                new_status = st.selectbox(
                    "ステータス変更",
                    ["new", "applied", "interview", "rejected", "closed"],
                    index=["new","applied","interview","rejected","closed"].index(
                        job.get("status", "new")
                    ),
                    key=f"status_sel_{selected_id}",
                )
            with col_s2:
                memo = st.text_input("メモ", value=job.get("memo") or "", key=f"memo_{selected_id}")
            if st.button("ステータス更新", key=f"update_{selected_id}"):
                db.update_job_status(selected_id, new_status, memo)
                st.success("更新しました")
                st.rerun()

        with tab2:
            st.text_area("元メール本文", job.get("raw_body") or "", height=400)

        with tab3:
            if job.get("block_reason"):
                st.warning(f"この案件はNG判定です: {job['block_reason']}\n強制的に応募メールを生成することもできますが、推奨しません。")

            if st.button("📝 応募メール生成", type="primary", key=f"compose_{selected_id}"):
                with st.spinner("Claude APIでメール生成中..."):
                    result = generate_application_mail(job)
                    st.session_state[f"composed_{selected_id}"] = result

            composed = st.session_state.get(f"composed_{selected_id}")
            if composed:
                st.write("**件名:**")
                st.code(composed.get("subject", ""), language=None)
                st.write("**本文:**")
                st.text_area("本文（コピーしてThunderbirdに貼り付け）",
                             composed.get("body", ""), height=400,
                             key=f"body_area_{selected_id}")
                st.caption("📋 上のテキストエリアを全選択してコピーし、Thunderbirdで新規メールとして送信してください。")

                if st.button("✅ 応募済みにする", key=f"mark_applied_{selected_id}"):
                    db.update_job_status(selected_id, "applied",
                                         f"応募メール生成 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
                    st.success("ステータスを「応募済」に更新しました")
                    st.rerun()
