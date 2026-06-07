"""
fetch_mails.py - 案件メールAPI -> Claude API解析 -> SQLite保存

【仕組み】
社内の「案件メールシステム」が公開しているWeb API
(https://www.iroha-keikaku.com/system/anken_mail_sys/public/api) から
日付範囲を指定してメールを検索・取得する。
認証は事前共有のアクセストークンによるBearer認証。

実行方法:
    uv run fetch_mails.py              # 過去7日分
    uv run fetch_mails.py --days 30    # 過去30日分

前提:
    - ANKEN_MAIL_API_TOKEN, ANTHROPIC_API_KEY を .env に設定済みであること

定期実行（Windowsタスクスケジューラ）:
    プログラム: uv.exe のフルパス
    引数:       run python fetch_mails.py
    開始場所:   C:\\path\\to\\ses-matcher
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Iterator

import anthropic
import httpx
from dotenv import load_dotenv

import db

sys.stdout.reconfigure(encoding='utf-8')

load_dotenv()

# ───────────────────────────────────────
# 設定
# ───────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
FETCH_DAYS         = int(os.environ.get("FETCH_DAYS_BACK", "7"))

ANKEN_MAIL_API_BASE_URL = "https://www.iroha-keikaku.com/system/anken_mail_sys/public/api"
ANKEN_MAIL_API_TOKEN    = os.environ.get("ANKEN_MAIL_API_TOKEN", "")

# 検索APIは「総件数100件超」だとHTTP 400になるため、日単位に区切って取得する
SEARCH_PAGE_LIMIT = 100

# 要員紹介メール（案件ではなく人材売込み）を件名から除外するためのパターン
# 検索APIの exclude パラメータ（件名一致で除外）にもそのまま渡し、取得件数自体を絞り込む
EXCLUDE_SUBJECT_PATTERNS = [
    "要員のご紹介", "ご紹介です！", "人材のご紹介",
    "経歴書（スキルシート）", "スキルシート添付", "ご送付依頼",
]

# 案件メール判定キーワード（subject+bodyに2件以上含まれていれば案件メールとみなす）
JOB_KEYWORDS = [
    "案件", "エンジニア募集", "Java", "SpringBoot", "springboot", "spring",
    "必須", "尚可", "単価", "面談",
]

# ───────────────────────────────────────
# 案件メールAPI クライアント
# ───────────────────────────────────────

class AnkenMailAPIClient:
    def __init__(self):
        if not ANKEN_MAIL_API_TOKEN:
            raise ValueError("ANKEN_MAIL_API_TOKEN が未設定です。.env を確認してください。")
        self.base_url = ANKEN_MAIL_API_BASE_URL
        self.headers  = {
            "Authorization": f"Bearer {ANKEN_MAIL_API_TOKEN}",
            "Accept":        "application/json",
        }
        print(f"[api] 案件メールAPI接続: {self.base_url}")

    def _get(self, path: str, params: dict) -> dict:
        resp = httpx.get(
            f"{self.base_url}/{path}",
            headers=self.headers,
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"API error: {data}")
        return data

    def search(self, date_from: str, date_to: str, offset: int = 0, limit: int = SEARCH_PAGE_LIMIT) -> dict:
        """メール一覧検索API (/search.php)"""
        return self._get("search.php", {
            "date_from": date_from,
            "date_to":   date_to,
            "exclude":   ",".join(EXCLUDE_SUBJECT_PATTERNS),
            "limit":     limit,
            "offset":    offset,
        })

    def search_all(self, date_from: str, date_to: str) -> list[dict]:
        """
        指定期間のメール一覧をすべて取得する。
        「総件数が100件を超えるとHTTP 400」という制約があるため、
        まず期間全体で検索を試み、400が返ってきた場合は1日単位に分割して取得する。
        """
        try:
            first = self.search(date_from, date_to)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400 and date_from != date_to:
                print(f"[fetch]   {date_from}〜{date_to}: 100件超のため日単位に分割して再取得します")
                items: list[dict] = []
                for day in _iter_dates(date_from, date_to):
                    items.extend(self.search_all(day, day))
                return _dedupe_by_id(items)
            if e.response.status_code == 400:
                print(f"[fetch]   {date_from}: 該当件数が100件を超えるためスキップします（絞り込みが必要です）")
                return []
            raise

        items = list(first.get("data", []))
        total = first.get("total_count", len(items))
        offset = len(items)
        while offset < total:
            page = self.search(date_from, date_to, offset=offset)
            page_items = page.get("data", [])
            if not page_items:
                break
            items.extend(page_items)
            offset += len(page_items)
        return items

    def get_detail(self, mail_id: int) -> dict:
        """メール詳細取得API (/mail_detail.php)"""
        data = self._get("mail_detail.php", {"id": mail_id})
        return data.get("data", {})


def _iter_dates(date_from: str, date_to: str) -> Iterator[str]:
    d   = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    while d <= end:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def _dedupe_by_id(items: list[dict]) -> list[dict]:
    seen: set = set()
    result = []
    for item in items:
        mid = item.get("id")
        if mid not in seen:
            seen.add(mid)
            result.append(item)
    return result


# ───────────────────────────────────────
# メール取得
# ───────────────────────────────────────

def fetch_messages(days_back: int) -> list[dict]:
    client = AnkenMailAPIClient()
    messages = []
    seen_subjects: set[str] = set()  # 今回実行内での件名重複排除用

    date_to   = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    print(f"[fetch] 検索期間: {date_from} 〜 {date_to}")

    try:
        items = client.search_all(date_from, date_to)
    except Exception as e:
        print(f"[fetch]   検索APIエラー: {e}")
        return messages

    print(f"[fetch]   {len(items)}件")

    for item in items:
        # APIのメールID（システム内一意）をそのまま重複排除キーとして利用する
        message_id = str(item.get("id", ""))
        subject    = item.get("subject", "")

        # 一覧APIは本文を含まない（メタデータのみ）ため、件名だけで判定できる
        # 「要員紹介メール」を先に除外し、詳細取得コストを節約する
        # （検索APIの exclude パラメータでもサーバー側で大半は絞り込み済み）
        if is_excluded_subject(subject):
            continue

        # message_id の重複チェック（DB照合）
        if db.is_duplicate(message_id):
            continue

        # 正規化件名の重複チェック（転送・ML転送による同一案件を除外）
        norm = normalize_subject(subject)
        if norm in seen_subjects:
            print(f"[fetch]   重複スキップ（転送）: {subject[:50]}")
            continue
        seen_subjects.add(norm)

        try:
            detail = client.get_detail(item["id"])
        except Exception as e:
            print(f"[fetch]   詳細取得エラー (id={message_id}): {e}")
            continue

        body = detail.get("body_text", "")
        if not is_job_mail(subject, body):
            continue

        messages.append({
            "message_id":  message_id,
            "subject":     subject,
            "sender_name": detail.get("from_name", "") or "",
            "sender_addr": detail.get("from_address", "") or "",
            "received_at": detail.get("received_at", ""),
            "body":        body,
        })

    return messages


def normalize_subject(subject: str) -> str:
    """件名を正規化して重複検出に使う（転送・MLプレフィックスを除去）"""
    import re as _re
    s = subject
    s = _re.sub(r"^(\[.*?\]\s*)+", "", s)
    s = _re.sub(r"^(Fwd?:\s*|Re:\s*|FW:\s*)+", "", s, flags=_re.IGNORECASE)
    return s.strip()


def is_excluded_subject(subject: str) -> bool:
    """要員紹介メール（案件ではなく人材売込み）を件名だけで判定して除外する"""
    return any(pat in subject for pat in EXCLUDE_SUBJECT_PATTERNS)


def is_job_mail(subject: str, body: str) -> bool:
    if is_excluded_subject(subject):
        return False
    text = subject + body
    return sum(1 for k in JOB_KEYWORDS if k in text) >= 2


# ───────────────────────────────────────
# Claude API 解析
# ───────────────────────────────────────

ANALYSIS_PROMPT = """\
あなたはSES（システムエンジニアリングサービス）の案件メールを構造化するAIです。
以下のメール本文から案件情報を抽出し、JSONのみを返してください。
説明文・前置き・マークダウン記法は一切不要です。

# 候補者情報（マッチング判定に使用）
- Javaエンジニア、26歳、実務経験約1.5〜2年
- 派遣業許可なし（準委任契約のみ対応可）
- Oracle認定Javaプログラマ Silver SE 保有
- SpringBoot, Vue.js, MySQL等の経験あり

# 抽出するJSON形式
{{
  "job_name": "案件名",
  "client_company": "発注元企業名（不明なら null）",
  "location": "勤務地（最寄り駅や区名）",
  "remote_type": "フルリモート / ハイブリッド / 常駐 のいずれか",
  "start_date": "開始時期（テキストのまま）",
  "min_years_req": 必須経験年数（整数、不明なら0）,
  "unit_price_min": 単価下限（万円整数、不明なら0）,
  "unit_price_max": 単価上限（万円整数、不明なら0）,
  "age_restriction": "年齢制限テキスト（なければ null）",
  "contract_type": "派遣 / 準委任 / 不明 のいずれか",
  "required_skills": ["必須スキルをリスト化"],
  "preferred_skills": ["尚可スキルをリスト化"],
  "notes": "その他重要な備考（最大100文字）",
  "match_score": マッチスコア0〜100,
  "recommend": true または false,
  "block_reason": "NGの理由。OKなら null"
}}

# マッチスコア計算ルール
block_reason を先に判定。ある場合は match_score=0, recommend=false。
block_reason の条件:
  - contract_type="派遣" -> "派遣免許必要のためNG"
  - min_years_req >= 5   -> "必須年数5年以上（実務1.5年）でNG"
  - age_restriction に「若手不可」を含む -> "年齢制限：若手不可でNG"
block_reason がない場合のスコア加算:
  - Java/SpringBoot が required_skills に含まれる: +30
  - min_years_req <= 2: +25 / min_years_req == 3: +10
  - フルリモートまたはハイブリッド: +15
  - unit_price_min >= 50: +10
  - Vue.js/MySQL/PostgreSQL が required/preferred に含まれる: +10
  - 面談1回: +10
recommend = match_score >= 40

# メール本文
{body}
"""


def analyze_with_claude(body: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = ANALYSIS_PROMPT.format(body=body[:4000])
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if not json_match:
        raise ValueError(f"JSON抽出失敗: {raw[:200]}")
    return json.loads(json_match.group())


# ───────────────────────────────────────
# ユーティリティ
# ───────────────────────────────────────

def parse_received_at(value: str) -> str:
    """APIの 'YYYY-MM-DD HH:MM:SS' 形式をISO8601形式に変換する"""
    if not value:
        return datetime.now().isoformat()
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").isoformat()
    except ValueError:
        return str(value)


# ───────────────────────────────────────
# メイン
# ───────────────────────────────────────

def run(days_back: int = FETCH_DAYS):
    db.init_db()
    print(f"\n[fetch_mails] 取込開始 (過去{days_back}日 / 案件メールAPI経由)")

    try:
        messages = fetch_messages(days_back)
    except ValueError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    print(f"[fetch_mails] 案件メール候補: {len(messages)}件\n")

    new_count = skip_count = error_count = 0

    for m in messages:
        mid = m["message_id"]
        if db.is_duplicate(mid):
            skip_count += 1
            continue

        print(f"  -> 解析: {m['subject'][:55]}")
        try:
            result = analyze_with_claude(m["body"])
        except Exception as e:
            print(f"     [ERROR] {e}")
            error_count += 1
            continue

        row = {
            "message_id":       mid,
            "received_at":      parse_received_at(m["received_at"]),
            "subject":          m["subject"],
            "sender_email":     m["sender_addr"],
            "sender_name":      m["sender_name"],
            "raw_body":         m["body"],
            "job_name":         result.get("job_name"),
            "client_company":   result.get("client_company"),
            "location":         result.get("location"),
            "remote_type":      result.get("remote_type"),
            "start_date":       result.get("start_date"),
            "min_years_req":    result.get("min_years_req", 0),
            "unit_price_min":   result.get("unit_price_min", 0),
            "unit_price_max":   result.get("unit_price_max", 0),
            "age_restriction":  result.get("age_restriction"),
            "contract_type":    result.get("contract_type", "不明"),
            "required_skills":  json.dumps(result.get("required_skills", []), ensure_ascii=False),
            "preferred_skills": json.dumps(result.get("preferred_skills", []), ensure_ascii=False),
            "notes":            result.get("notes"),
            "match_score":      result.get("match_score", 0),
            "recommend":        1 if result.get("recommend") else 0,
            "block_reason":     result.get("block_reason"),
            "status":           "new",
        }
        db.insert_job(row)
        flag = "推奨" if row["recommend"] else f"NG({row['block_reason']})"
        print(f"     [{flag}] score:{row['match_score']} 契約:{row['contract_type']} 必須:{row['min_years_req']}年")
        new_count += 1

    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO fetch_log(new_count,skip_count,error_count,message) VALUES(?,?,?,?)",
            (new_count, skip_count, error_count,
             f"完了: 新規{new_count} スキップ{skip_count} エラー{error_count}"),
        )

    print(f"\n[fetch_mails] 完了 -- 新規:{new_count} スキップ:{skip_count} エラー:{error_count}")
    return new_count, skip_count, error_count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SES案件メール取込バッチ (案件メールAPI経由)")
    parser.add_argument("--days", type=int, default=FETCH_DAYS, help="取込日数（デフォルト7）")
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        print("[ERROR] ANTHROPIC_API_KEY が未設定です。.env を確認してください。")
        sys.exit(1)
    if not ANKEN_MAIL_API_TOKEN:
        print("[ERROR] ANKEN_MAIL_API_TOKEN が未設定です。.env を確認してください。")
        sys.exit(1)

    run(args.days)
