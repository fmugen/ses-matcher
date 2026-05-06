"""
fetch_mails.py - Thunderbird MCP (HTTP) -> Claude API解析 -> SQLite保存

【仕組み】
Thunderbird MCP拡張が localhost:8765 でHTTPサーバーを起動している。
接続情報（port/token）は %TEMP%/thunderbird-mcp/connection.json に書かれる。
PythonからHTTP POSTで直接JSON-RPC呼び出しが可能。

実行方法:
    uv run fetch_mails.py              # 過去7日分
    uv run fetch_mails.py --days 30    # 過去30日分

前提:
    - Thunderbird が起動中であること（HTTPサーバーが立ち上がる）
    - ANTHROPIC_API_KEY を .env に設定済みであること

定期実行（Windowsタスクスケジューラ）:
    プログラム: uv.exe のフルパス
    引数:       run python fetch_mails.py
    開始場所:   C:\\path\\to\\ses-matcher
    ※ Thunderbird が起動している時間帯に実行すること
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import httpx
from dotenv import load_dotenv

import db

load_dotenv()

# ───────────────────────────────────────
# 設定
# ───────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FETCH_DAYS        = int(os.environ.get("FETCH_DAYS_BACK", "7"))

# 取込対象フォルダ URI（listFolders で確認した値）
TARGET_FOLDER_URIS = [
    "imap://m-fujii%40iroha-keikaku.com@mail.iroha-keikaku.com/INBOX",
    "imap://m-fujii%40iroha-keikaku.com@mail.iroha-keikaku.com/INBOX/[office] ",
]

# ───────────────────────────────────────
# Thunderbird MCP HTTP クライアント
# ───────────────────────────────────────

def load_mcp_connection() -> tuple[int, str]:
    """
    %TEMP%/thunderbird-mcp/connection.json からポートとトークンを読む。
    Thunderbird 起動時に拡張が自動生成するファイル。
    """
    conn_path = Path(tempfile.gettempdir()) / "thunderbird-mcp" / "connection.json"
    if not conn_path.exists():
        raise FileNotFoundError(
            f"connection.json が見つかりません: {conn_path}\n"
            "Thunderbird が起動中か確認してください。"
        )
    data = json.loads(conn_path.read_text(encoding="utf-8"))
    port  = data.get("port", 8765)
    token = data.get("token", "")
    if not token:
        raise ValueError("connection.json にトークンがありません。")
    return port, token


class ThunderbirdMCPClient:
    def __init__(self):
        self.port, self.token = load_mcp_connection()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.headers  = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type":  "application/json",
        }
        self._req_id = 0
        print(f"[mcp] Thunderbird MCP 接続: port={self.port}")

    def call(self, method: str, params: dict) -> dict:
        """JSON-RPC 2.0 呼び出し"""
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id":      self._req_id,
            "method":  method,
            "params":  params,
        }
        resp = httpx.post(
            self.base_url,
            headers=self.headers,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result", {})

    def get_recent_messages(self, folder_uri: str, days_back: int) -> list[dict]:
        result = self.call("tools/call", {
            "name":      "getRecentMessages",
            "arguments": {
                "folderPath": folder_uri,
                "daysBack":   days_back,
                "maxResults": 200,
            },
        })
        # result は {"content": [{"type":"text","text":"[...]"}]} の形
        return _parse_tool_result(result)

    def get_message(self, folder_uri: str, message_id: str) -> dict:
        result = self.call("tools/call", {
            "name":      "getMessage",
            "arguments": {
                "folderPath": folder_uri,
                "messageId":  message_id,
                "bodyFormat": "text",
            },
        })
        return _parse_tool_result(result)


def _parse_tool_result(result) -> dict | list:
    """tools/call の返却値からコンテンツを取り出す"""
    if isinstance(result, (dict, list)):
        # すでにパース済みの場合
        if isinstance(result, list):
            return result
        # {"content": [{"type":"text","text":"..."}]} 形式
        content = result.get("content", [])
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except json.JSONDecodeError:
                    return {"raw": block["text"]}
    return {}


# ───────────────────────────────────────
# メール取得
# ───────────────────────────────────────

def fetch_messages(days_back: int) -> list[dict]:
    client = ThunderbirdMCPClient()
    messages = []
    seen_subjects: set[str] = set()  # 今回実行内での件名重複排除用

    for folder_uri in TARGET_FOLDER_URIS:
        folder_name = folder_uri.split("/")[-1].strip()
        print(f"[fetch] フォルダ: {folder_name}")

        try:
            headers = client.get_recent_messages(folder_uri, days_back)
        except Exception as e:
            print(f"[fetch]   getRecentMessages エラー: {e}")
            continue

        if not isinstance(headers, list):
            headers = headers.get("messages", []) if isinstance(headers, dict) else []

        print(f"[fetch]   {len(headers)}件")

        for h in headers:
            msg_id  = h.get("id", "")
            subject = h.get("subject", "")
            preview = h.get("preview", "")

            # プレビューで先にフィルタ（本文取得コストを節約）
            if not is_job_mail(subject, preview):
                continue

            # message_id の重複チェック（DB照合）
            if db.is_duplicate(msg_id):
                continue

            # 正規化件名の重複チェック（転送・ML転送による同一案件を除外）
            norm = normalize_subject(subject)
            if norm in seen_subjects:
                print(f"[fetch]   重複スキップ（転送）: {subject[:50]}")
                continue
            seen_subjects.add(norm)

            try:
                detail = client.get_message(folder_uri, msg_id)
            except Exception as e:
                print(f"[fetch]   getMessage エラー ({msg_id[:20]}...): {e}")
                continue

            body = detail.get("body", "") if isinstance(detail, dict) else ""
            if not is_job_mail(subject, body):
                continue

            messages.append({
                "message_id": msg_id,
                "subject":    subject,
                "sender":     detail.get("author", "") if isinstance(detail, dict) else "",
                "date_val":   detail.get("date")       if isinstance(detail, dict) else None,
                "body":       body,
            })

    return messages


def normalize_subject(subject: str) -> str:
    """件名を正規化して重複検出に使う（転送・MLプレフィックスを除去）"""
    import re as _re
    s = subject
    s = _re.sub(r"^(\[.*?\]\s*)+", "", s)
    s = _re.sub(r"^(Fwd?:\s*|Re:\s*|FW:\s*)+", "", s, flags=_re.IGNORECASE)
    return s.strip()


def is_job_mail(subject: str, body: str) -> bool:
    # 要員紹介メール（案件ではなく人材売込み）を除外
    exclude_patterns = [
        "要員のご紹介", "ご紹介です！", "人材のご紹介",
        "経歴書（スキルシート）", "スキルシート添付", "ご送付依頼",
    ]
    for pat in exclude_patterns:
        if pat in subject:
            return False

    keywords = [
        "案件", "エンジニア募集", "Java", "SpringBoot",
        "必須", "尚可", "単価", "面談",
    ]
    text = subject + body
    return sum(1 for k in keywords if k in text) >= 2


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

def parse_sender(sender_raw: str) -> tuple[str, str]:
    m = re.match(r'^(.*?)\s*<(.+?)>$', sender_raw.strip())
    if m:
        return m.group(1).strip().strip('"'), m.group(2).strip()
    return "", sender_raw.strip()


def parse_date(date_val) -> str:
    if not date_val:
        return datetime.now().isoformat()
    if isinstance(date_val, (int, float)):
        return datetime.fromtimestamp(date_val / 1000).isoformat()
    return str(date_val)


# ───────────────────────────────────────
# メイン
# ───────────────────────────────────────

def run(days_back: int = FETCH_DAYS):
    db.init_db()
    print(f"\n[fetch_mails] 取込開始 (過去{days_back}日 / Thunderbird MCP HTTP経由)")

    try:
        messages = fetch_messages(days_back)
    except FileNotFoundError as e:
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

        sender_name, sender_email = parse_sender(m["sender"])
        row = {
            "message_id":       mid,
            "received_at":      parse_date(m["date_val"]),
            "subject":          m["subject"],
            "sender_email":     sender_email,
            "sender_name":      sender_name,
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
    parser = argparse.ArgumentParser(description="SES案件メール取込バッチ (Thunderbird MCP HTTP)")
    parser.add_argument("--days", type=int, default=FETCH_DAYS, help="取込日数（デフォルト7）")
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        print("[ERROR] ANTHROPIC_API_KEY が未設定です。.env を確認してください。")
        sys.exit(1)

    run(args.days)
