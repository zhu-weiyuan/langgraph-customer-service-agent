from agent import memory as m
c = m._get_connection()
r = c.execute(
    "SELECT user_id, title, message_count FROM sessions WHERE user_id=?",
    ("zwy",)
).fetchall()
print("zwy sessions:", len(r))
for x in r:
    print(f'  title="{x["title"]}" msgs={x["message_count"]}')
c.close()
