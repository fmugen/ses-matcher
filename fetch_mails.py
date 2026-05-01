"""
fetch_mails.py - メール取得 -> Claude API解析 -> SQLite保存

実行方法:
    uv run fetch_mails.py              # 過去7日分を取込
    uv run fetch_mails.py --days 30    # 過去30日分

定期実行:
    Windowsタスクスケジューラ で登録
"""

import argparse
import imaplib
import email
import email.header
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

import db
from candidate_profile import CANDIDATE

load_dotenv()

# ───────────────────────────────────────
# 定数・設定
# ───────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
IMAP_HOST     = os.environ.get("IMAP_HOST", "mail.iroha-keikaku.com")
IMAP_PORT     = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER     = os.environ.get("IMAP_USER", "m-fujii@iroha-keikaku.com")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
FETCH_DAYS    = int(os.environ.get("FETCH_DAYS_BACK", "7"))

# 案件メールを含むフォルダ（IMAP URI ではなく名前で指定）
TARGET_FOLDERS = ["INBOX", "INBOX/[office]"]

# ───────────────────────────────────────
# メール取得
# ───────────────────────────────────────

def decode_header_str(raw: str) -> str:
    parts = email.header.decode_header(raw)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return "".join(decoded)


def get_text_body(msg: email.message.Message) -> str:
    """text/plain パートを再帰的に取得"""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace") if payload else ""
    return ""


def fetch_imap_messages(days_back: int) -> list[dict]:
    """IMAPで直接メール取得"""
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%d-%b-%Y")
    messages = []

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASSWORD)
    except Exception as e:
        print(f"[fetch] IMAP接続失敗: {e}")
        return messages

    for folder in TARGET_FOLDERS:
        try:
            status, _ = mail.select(folder, readonly=True)
            if status != "OK":
                print(f"[fetch] フォルダ選択失敗: {folder}")
                continue

            _, data = mail.search(None, f'(SINCE "{since}")')
            ids = data[0].split()
            print(f"[fetch] {folder}: {len(ids)}件")

            for uid in ids:
                _, msg_data = mail.fetch(uid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                message_id = msg.get("Message-ID", "").strip()
                if not message_id:
                    continue

                subject = decode_header_str(msg.get("Subject", ""))
                sender  = decode_header_str(msg.get("From", ""))
                date_str = msg.get("Date", "")
                body = get_text_body(msg)

                # 案件メールかどうか簡易判定
                if not is_job_mail(subject, body):
                    continue

                messages.append({
                    "message_id": message_id,
                    "subject":    subject,
                    "sender":     sender,
                    "date_str":   date_str,
                    "body":       body,
                })
        except Exception as e:
            print(f"[fetch] {folder} 処理エラー: {e}")

    mail.logout()
    return messages


def is_job_mail(subject: str, body: str) -> bool:
    """案件紹介メールかどうかの簡易フィルタ"""
    keywords = ["案件", "要員", "エンジニア募集", "Java", "SpringBoot",
                "スキル", "必須", "尚可", "単価", "面談"]
    text = subject + body
    return sum(1 for k in keywords if k in text) >= 2


# ───────────────────────────────────────
# Claude API 解析
# ───────────────────────────────────────

ANALYSIS_PROMPT = """
あなたはSES（システムエンジニアリングサービス）の案件メールを構造化するAIです。
以下のメール本文から案件情報を抽出し、**JSONのみ**を返してください。
説明文・前置き・マークダウン記法（```json など）は一切不要です。

# 候補者情報（マッチング判定に使用）
- Javaエンジニア、26歳、実務経験約1.5〜2年
- 派遣業許可なし（準委任契約のみ対応可）
- Oracle認定Javaプログラマ Silver SE 保有
- SpringBoot, Vue.js, MySQL等の経験あり

# 抽出するJSON形式
{
  "job_name": "案件名（文字列）",
  "client_company": "発注元企業名（不明なら null）",
  "location": "勤務地（最寄り駅や区名）",
  "remote_type": "フルリモート / ハイブリッド / 常駐 のいずれか",
  "start_date": "開始時期（テキストのまま）",
  "min_years_req": 必須経験年数（整数。「3年以上」なら3。不明なら0）,
  "unit_price_min": 単価下限（万円の整数。「60〜70万」なら60。不明なら0）,
  "unit_price_max": 単価上限（万円の整数。不明なら0）,
  "age_restriction": "年齢制限テキスト（例: '若手不可', '50代まで'。なければ null）",
  "contract_type": "派遣 / 準委任 / 不明 のいずれか",
  "required_skills": ["必須スキルをリスト化"],
  "preferred_skills": ["尚可スキルをリスト化"],
  "notes": "その他重要な備考（最大100文字）",
  "match_score": マッチスコア0〜100（後述ルールで計算）,
  "recommend": true または false,
  "block_reason": "NGの場合の理由。OKなら null"
}

# マッチスコア計算ルール
- 必ず block_reason を先に判定すること
- block_reason がある場合: match_score=0, recommend=false
- block_reason の判定条件:
  - contract_type="派遣" → "派遣免許必要のためNG"
  - min_years_req >= 5 → "必須年数5年以上（実務1.5年）でNG"
  - age_restriction に「若手不可」を含む → "年齢制限：若手不可でNG"
  - 外国籍不可 かつ（今回は日本国籍なので影響なし）
- block_reason がない場合のスコア加算:
  - Java/SpringBoot が required_skills に含まれる: +30
  - min_years_req <= 2: +25（満たせる）/ min_years_req == 3: +10（△）
  - フルリモートまたはハイブリッド: +15
  - unit_price_min >= 50: +10
  - Vue.js/MySQL/PostgreSQL が required_skills または preferred_skills に含まれる: +10
  - 面談1回: +10
- recommend = match_score >= 40

# メール本文
{body}
"""


def analyze_with_claude(body: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = ANALYSIS_PROMPT.format(body=body[:4000])  # トークン節約

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()

    # JSON部分だけ抽出（念のため）
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if not json_match:
        raise ValueError(f"JSON抽出失敗: {raw[:200]}")

    return json.loads(json_match.group())


# ───────────────────────────────────────
# メイン処理
# ───────────────────────────────────────

def parse_sender(sender_raw: str) -> tuple[str, str]:
    """'名前 <email>' を分解"""
    m = re.match(r'^(.*?)\s*<(.+?)>$', sender_raw.strip())
    if m:
        return m.group(1).strip().strip('"'), m.group(2).strip()
    return "", sender_raw.strip()


def parse_date(date_str: str) -> str:
    """RFC2822 → ISO8601"""
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        return dt.isoformat()
    except Exception:
        return datetime.now().isoformat()


def run(days_back: int = FETCH_DAYS):
    db.init_db()

    print(f"\n[fetch_mails] 取込開始 (過去{days_back}日)")
    messages = fetch_imap_messages(days_back)
    print(f"[fetch_mails] 案件メール候補: {len(messages)}件")

    new_count = skip_count = error_count = 0

    for m in messages:
        mid = m["message_id"]

        # 重複チェック
        if db.is_duplicate(mid):
            skip_count += 1
            continue

        print(f"  → 解析中: {m['subject'][:50]}")
        try:
            result = analyze_with_claude(m["body"])
        except Exception as e:
            print(f"  [ERROR] Claude解析失敗: {e}")
            error_count += 1
            continue

        sender_name, sender_email = parse_sender(m["sender"])

        row = {
            "message_id":      mid,
            "received_at":     parse_date(m["date_str"]),
            "subject":         m["subject"],
            "sender_email":    sender_email,
            "sender_name":     sender_name,
            "raw_body":        m["body"],
            "job_name":        result.get("job_name"),
            "client_company":  result.get("client_company"),
            "location":        result.get("location"),
            "remote_type":     result.get("remote_type"),
            "start_date":      result.get("start_date"),
            "min_years_req":   result.get("min_years_req", 0),
            "unit_price_min":  result.get("unit_price_min", 0),
            "unit_price_max":  result.get("unit_price_max", 0),
            "age_restriction": result.get("age_restriction"),
            "contract_type":   result.get("contract_type", "不明"),
            "required_skills": json.dumps(result.get("required_skills", []), ensure_ascii=False),
            "preferred_skills":json.dumps(result.get("preferred_skills", []), ensure_ascii=False),
            "notes":           result.get("notes"),
            "match_score":     result.get("match_score", 0),
            "recommend":       1 if result.get("recommend") else 0,
            "block_reason":    result.get("block_reason"),
            "status":          "new",
        }

        db.insert_job(row)
        flag = "✅ 推奨" if row["recommend"] else f"❌ NG（{row['block_reason']}）"
        print(f"     {flag} スコア:{row['match_score']} 契約:{row['contract_type']} 必須年数:{row['min_years_req']}年")
        new_count += 1

    # 実行ログ保存
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO fetch_log(new_count,skip_count,error_count,message) VALUES(?,?,?,?)",
            (new_count, skip_count, error_count,
             f"完了: 新規{new_count} スキップ{skip_count} エラー{error_count}"),
        )

    print(f"\n[fetch_mails] 完了 — 新規:{new_count} スキップ:{skip_count} エラー:{error_count}")
    return new_count, skip_count, error_count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SES案件メール取込バッチ")
    parser.add_argument("--days", type=int, default=FETCH_DAYS, help="取込日数（デフォルト7）")
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        print("[ERROR] ANTHROPIC_API_KEY が未設定です。.env を確認してください。")
        sys.exit(1)
    if not IMAP_PASSWORD:
        print("[ERROR] IMAP_PASSWORD が未設定です。.env を確認してください。")
        sys.exit(1)

    run(args.days)
