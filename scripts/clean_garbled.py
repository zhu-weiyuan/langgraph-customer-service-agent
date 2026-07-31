# -*- coding: utf-8 -*-
"""清理数据库中残留的乱码 product_interests"""
import psycopg, os
os.environ['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'
c = psycopg.connect(os.environ['DATABASE_URL'], autocommit=True)

# 删掉所有含 ???? 格式乱码的行（这些是之前编码损坏时匹配进去的）
c.execute("""
    DELETE FROM user_preferences 
    WHERE user_id='zwy' AND product_interests::text LIKE '%?%?%?%'
""")
print("Deleted garbled rows (pattern ???)")

remaining = c.execute("SELECT COUNT(*) FROM user_preferences WHERE user_id='zwy'").fetchone()[0]
print("Remaining: %d rows" % remaining)
for r in c.execute("SELECT session_id, product_interests FROM user_preferences WHERE user_id='zwy'").fetchall():
    print("  [%s] %s" % (r[0][:30], r[1]))

c.close()
