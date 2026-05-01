"""
db.py - SQLiteスキーマ定義とDB操作ヘルパー
"""
import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "ses_matcher.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db_conn():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """テーブル作成（冪等）"""
    with db_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id      TEXT UNIQUE NOT NULL,   -- メールのMessage-ID（重複排除キー）
            received_at     TEXT NOT NULL,           -- メール受信日時 ISO8601
            subject         TEXT,
            sender_email    TEXT,
            sender_name     TEXT,
            raw_body        TEXT,                    -- メール本文そのまま

            -- AI解析結果
            job_name        TEXT,
            client_company  TEXT,
            location        TEXT,
            remote_type     TEXT,                    -- フルリモート/ハイブリッド/常駐
            start_date      TEXT,
            min_years_req   INTEGER,                 -- 必須経験年数（数値化）
            unit_price_min  INTEGER,                 -- 単価下限（万円）
            unit_price_max  INTEGER,
            age_restriction TEXT,                    -- 例: "若手不可", "50代まで" など
            contract_type   TEXT,                    -- 派遣/準委任/不明
            required_skills TEXT,                    -- JSON配列 ["Java","SpringBoot",...]
            preferred_skills TEXT,                   -- JSON配列
            notes           TEXT,                    -- その他備考

            -- マッチング判定
            match_score     INTEGER DEFAULT 0,       -- 0-100
            recommend       INTEGER DEFAULT 0,       -- 1=推奨, 0=非推奨
            block_reason    TEXT,                    -- NG理由 ("派遣免許必要" など)

            -- 営業ステータス
            status          TEXT DEFAULT 'new',      -- new/applied/interview/rejected/closed
            applied_at      TEXT,
            memo            TEXT,

            created_at      TEXT DEFAULT (datetime('now','localtime')),
            updated_at      TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_recommend  ON jobs(recommend);
        CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);

        CREATE TABLE IF NOT EXISTS fetch_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at  TEXT DEFAULT (datetime('now','localtime')),
            new_count   INTEGER,
            skip_count  INTEGER,
            error_count INTEGER,
            message     TEXT
        );
        """)
    print(f"[db] initialized: {DB_PATH}")


def is_duplicate(message_id: str) -> bool:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None


def insert_job(data: dict) -> int:
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    with db_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO jobs ({cols}) VALUES ({placeholders})",
            list(data.values()),
        )
        return cur.lastrowid


def update_job_status(job_id: int, status: str, memo: str = None):
    with db_conn() as conn:
        if memo is not None:
            conn.execute(
                "UPDATE jobs SET status=?, memo=?, updated_at=datetime('now','localtime') WHERE id=?",
                (status, memo, job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
                (status, job_id),
            )


def get_jobs(status_filter: str = None, recommend_only: bool = False) -> list[dict]:
    where_clauses = []
    params = []
    if status_filter:
        where_clauses.append("status = ?")
        params.append(status_filter)
    if recommend_only:
        where_clauses.append("recommend = 1")

    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    with db_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_job_by_id(job_id: int) -> dict | None:
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


if __name__ == "__main__":
    init_db()
