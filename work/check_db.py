import sqlite3
c = sqlite3.connect('user_memory.db')
c.row_factory = sqlite3.Row
print("Latest sessions:")
for r in c.execute("SELECT session_id,message_count,last_active,title FROM sessions ORDER BY last_active DESC LIMIT 10").fetchall():
    sid = str(r["session_id"] or "")[:30]
    cnt2 = c.execute("SELECT COUNT(*) as c FROM conversation_history WHERE session_id=?", (r["session_id"],)).fetchone()["c"]
    print(f'  "{sid}" msg_cnt={r["message_count"]} hist_cnt={cnt2} last={str(r["last_active"])[:30]}')
