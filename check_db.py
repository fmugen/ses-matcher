import sqlite3

conn = sqlite3.connect('ses_matcher.db')
conn.row_factory = sqlite3.Row

print('=== 損保案件の重複確認 ===')
for row in conn.execute("""
    SELECT id, subject, job_name, message_id, received_at, sender_email
    FROM jobs
    WHERE job_name LIKE '%損保%' OR subject LIKE '%損保%'
    ORDER BY received_at
"""):
    print(f"ID:{row['id']} [{row['received_at'][:16]}]")
    print(f"  件名: {row['subject'][:60]}")
    print(f"  案件名: {row['job_name']}")
    print(f"  送信元: {row['sender_email']}")
    print(f"  message_id: {row['message_id'][:60]}")
    print()

print()
print('=== 全案件の重複確認（job_nameが同じもの） ===')
for row in conn.execute("""
    SELECT job_name, count(*) as cnt
    FROM jobs
    GROUP BY job_name
    HAVING cnt > 1
    ORDER BY cnt DESC
    LIMIT 15
"""):
    print(f"  {row['job_name']}: {row['cnt']}件")

print()
print('=== エラー件（job_nameがnull） ===')
for row in conn.execute("""
    SELECT subject, sender_email, received_at
    FROM jobs
    WHERE job_name IS NULL
    LIMIT 10
"""):
    print(f"  [{row['received_at'][:10]}] {row['subject'][:50]}")
    print(f"    from: {row['sender_email']}")

conn.close()
