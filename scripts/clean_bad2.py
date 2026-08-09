# -*- coding: utf-8 -*-
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from agent.runtime_db import database_url

conn = psycopg.connect(database_url(), autocommit=True)
USER_ID = "zwy"

r = conn.execute(
    "DELETE FROM conversation_history WHERE user_id = %s AND (bot_reply LIKE %s OR bot_reply LIKE %s)",
    (USER_ID, '感谢您的反馈%', '抱歉%')
)
print("Deleted: %d" % r.rowcount)

conn.execute("DELETE FROM sessions WHERE user_id = %s", (USER_ID,))
print("Sessions cleaned")

total = conn.execute(
    "SELECT COUNT(*) FROM conversation_history WHERE user_id = %s",
    (USER_ID,)
).fetchone()
print("Remaining: %d" % total[0])

conn.close()
