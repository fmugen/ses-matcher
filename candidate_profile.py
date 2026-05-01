"""
candidate_profile.py - Y.O.のプロフィール定数
応募メール生成・マッチング判定に使用
"""

CANDIDATE = {
    "name": "Y.O",
    "age": 26,
    "nearest_station": "JR南武線 尻手駅",
    "available_from": "2026年5月〜",
    "employment_type": "弊社正社員（プロパー）",
    "desired_rate": 50,          # 万円/月
    "desired_rate_note": "ご相談可",
    "work_style": "週5日・顧客先常駐可",
    "preferred_project": "Javaを用いたオンライン／バッチ開発",
    "preferred_team_size": "5名以上のチーム開発を希望",

    # 実務経験年数（マッチング判定に使用）
    "java_years": 1.5,           # 約1.5〜2年
    "springboot_years": 1.5,

    # スキルセット
    "skills": [
        "Java", "SpringBoot", "SpringMVC", "Spring",
        "C#.NET", "JavaScript", "Vue.js",
        "Oracle", "MySQL", "PostgreSQL",
        "Git", "JUnit", "selenium",
        "JP1",
    ],
    "certifications": [
        "Oracle Certified Java Programmer Silver SE",
        "日商簿記3級",
    ],
    "strengths": [
        "IT講師経験あり（対人コミュニケーション・説明力に定評）",
        "Oracle認定Javaプログラマ資格保有（技術的裏付け）",
        "製造業向け業務システムにてJavaEE基本設計〜テスト経験",
        "SpringBoot3 + MyBatis + MySQL を用いた実務経験あり",
    ],

    # NG条件（マッチング自動判定用）
    "constraints": {
        "has_dispatch_license": False,   # 派遣免許なし → 派遣契約案件はNG
        "nationality": "日本国籍",
        "max_commute_from": "神奈川県川崎市（尻手駅）",
    },
}

COMPANY = {
    "name": "株式会社いろは計画",
    "representative": "藤井 無限",
    "email": "m-fujii@iroha-keikaku.com",
    "phone": "090-8391-1179",
    "url": "https://iroha-keikaku.com",
    "address": "〒134-0084 東京都江戸川区東葛西5-13-1 マインコーポ葛西417",
    "signature": """╔══════════════════════════════════════╗
        🌟 中小企業を元気に！ 🌟

     株式会社 いろは計画　Iroha Keikaku
          藤井 無限（ふじい むげん）
    🌐https://www.iroha-keikaku.com/
╚═════════════════════════════════════╝""",
}
