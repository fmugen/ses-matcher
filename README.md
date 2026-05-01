# SES案件マッチャー

いろは計画 SES事業向け：案件メール自動取込・AI解析・応募メール生成システム

## セットアップ（5分）

```bash
# 1. 依存パッケージインストール
uv sync

# 2. 環境変数設定
cp .env.example .env
# .env を編集して ANTHROPIC_API_KEY と IMAP_PASSWORD を設定

# 3. DBの初期化
uv run python db.py

# 4. メール取込テスト（過去7日）
uv run python fetch_mails.py --days 7

# 5. UIを起動
uv run streamlit run app.py
```

ブラウザで http://localhost:8501 が開きます。

---

## 定期実行（Windowsタスクスケジューラ）

1. タスクスケジューラを開く（`taskschd.msc`）
2. 「基本タスクの作成」
3. トリガー: 毎日 09:00（と 12:00 の2つ作成推奨）
4. 操作: プログラムの開始
   - プログラム: `C:\Users\<ユーザー名>\.local\bin\uv.exe`
   - 引数: `run python C:\path\to\ses-matcher\fetch_mails.py`
   - 開始場所: `C:\path\to\ses-matcher`

---

## ファイル構成

```
ses-matcher/
├── fetch_mails.py       # バッチ: メール取込 → Claude解析 → DB保存
├── app.py               # Streamlit UI
├── db.py                # SQLiteスキーマ & ヘルパー
├── mail_composer.py     # 応募メール生成（Claude API）
├── candidate_profile.py # Y.O.のプロフィール定数
├── ses_matcher.db       # SQLite DB（自動生成）
├── pyproject.toml
├── .env                 # 環境変数（gitignore対象）
└── .env.example
```

---

## マッチング判定ロジック（自動NG条件）

| 条件 | 処理 |
|------|------|
| 契約形態が「派遣」 | ❌ NG（派遣免許なし） |
| 必須経験年数 5年以上 | ❌ NG（実務1.5年） |
| 「若手不可」の年齢制限 | ❌ NG |
| 上記以外 | スコア計算 → 40以上で推奨 |

---

## 今後の拡張（Ph.2）

- [ ] F-010 ダッシュボード（案件数推移グラフ）
- [ ] F-012 追客機能（各社とのやり取りDB管理）
- [ ] F-013 Thunderbird MCPからワンクリック送信
- [ ] F-014 アサインナビ連携（Playwright自動操作）
