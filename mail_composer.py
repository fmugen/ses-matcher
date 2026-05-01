"""
mail_composer.py - Claude APIを使って応募メール文面を生成
"""
import json
import os
import anthropic
from dotenv import load_dotenv
from candidate_profile import CANDIDATE, COMPANY

load_dotenv()


COMPOSE_PROMPT = """
あなたは日本語のビジネスメール作成の専門家です。
以下の案件情報と候補者プロフィールをもとに、SES要員の応募メールを作成してください。

# ルール
- 丁寧なビジネス日本語
- 必須スキルへの○/△回答を必ず含める
- 経験年数が不足する項目は「△」として、フォロー説明を添える（隠さない）
- 長くなりすぎない（本文300〜400文字程度）
- 件名も作成すること

# 案件情報
案件名: {job_name}
発注元: {client_company}
勤務地: {location}
リモート: {remote_type}
必須スキル: {required_skills}
尚可スキル: {preferred_skills}
単価: {unit_price_min}〜{unit_price_max}万円
契約形態: {contract_type}
宛先担当者: {sender_name}（{sender_email}）
備考: {notes}

# 候補者プロフィール
氏名: {cand_name}（{cand_age}歳・弊社正社員）
最寄り駅: {cand_station}
稼働開始: {cand_available}
希望単価: {cand_rate}万円（{cand_rate_note}）
スキル: {cand_skills}
資格: {cand_certs}
強み: {cand_strengths}

# 差出人情報
会社名: {comp_name}
担当者: {comp_rep}
メール: {comp_email}

# 出力形式（JSONのみ返すこと）
{{
  "subject": "件名",
  "body": "本文（署名なし）"
}}
"""


def generate_application_mail(job: dict) -> dict:
    """案件情報dictを渡して応募メール（件名＋本文）を返す"""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    required_skills = job.get("required_skills", "[]")
    if isinstance(required_skills, str):
        try:
            required_skills = json.loads(required_skills)
        except Exception:
            required_skills = [required_skills]

    preferred_skills = job.get("preferred_skills", "[]")
    if isinstance(preferred_skills, str):
        try:
            preferred_skills = json.loads(preferred_skills)
        except Exception:
            preferred_skills = [preferred_skills]

    prompt = COMPOSE_PROMPT.format(
        job_name       = job.get("job_name") or job.get("subject", "（案件名不明）"),
        client_company = job.get("client_company") or "不明",
        location       = job.get("location") or "不明",
        remote_type    = job.get("remote_type") or "不明",
        required_skills= "、".join(required_skills),
        preferred_skills= "、".join(preferred_skills),
        unit_price_min = job.get("unit_price_min") or 0,
        unit_price_max = job.get("unit_price_max") or 0,
        contract_type  = job.get("contract_type") or "不明",
        sender_name    = job.get("sender_name") or "ご担当者",
        sender_email   = job.get("sender_email") or "",
        notes          = job.get("notes") or "なし",
        cand_name      = CANDIDATE["name"],
        cand_age       = CANDIDATE["age"],
        cand_station   = CANDIDATE["nearest_station"],
        cand_available = CANDIDATE["available_from"],
        cand_rate      = CANDIDATE["desired_rate"],
        cand_rate_note = CANDIDATE["desired_rate_note"],
        cand_skills    = "、".join(CANDIDATE["skills"][:10]),
        cand_certs     = "、".join(CANDIDATE["certifications"]),
        cand_strengths = "／".join(CANDIDATE["strengths"][:2]),
        comp_name      = COMPANY["name"],
        comp_rep       = COMPANY["representative"],
        comp_email     = COMPANY["email"],
    )

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )

    import re
    raw = message.content[0].text.strip()
    json_match = re.search(r'\{[\s\S]*\}', raw)
    if not json_match:
        return {"subject": "（生成失敗）", "body": raw}

    result = json.loads(json_match.group())
    # 署名を付与
    result["body"] = result.get("body", "") + "\n\n" + COMPANY["signature"]
    return result
