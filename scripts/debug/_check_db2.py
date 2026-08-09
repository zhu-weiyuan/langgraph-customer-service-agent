"""Check database schema and data."""
import sqlite3

db_path = 'C:/Users/Administrator/.openclaw/workspace/langgraph-customer-service-agent/user_memory.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
for t in [r[0] for r in tables]:
    print(f'=== {t} ===')
    try:
        schema = conn.execute(f'SELECT sql FROM sqlite_master WHERE name=?', (t,)).fetchone()
        print(schema['sql'] if schema else 'no schema')
    except Exception as e:
        print(f'Error: {e}')

# Check all sessions with messages
print('\n=== Sessions with message_count ===')
for r in conn.execute('SELECT * FROM sessions').fetchall():
    print(dict(r))

# Check conversations table for zwy
print('\n=== Conversations table ===')
for r in conn.execute('SELECT * FROM conversations LIMIT 10').fetchall():
    print(dict(r))

# Check conversation_messages
print('\n=== conversation_messages schema ===')
try:
    print(conn.execute("SELECT sql FROM sqlite_master WHERE name='conversation_messages'").fetchone()['sql'])
except Exception as e:
    print(f'Error: {e}')

# Check conversation_messages
print('\n=== conversation_messages ===')
try:
    for r in conn.execute('SELECT * FROM conversation_messages LIMIT 20').fetchall():
        print(dict(r))
except Exception as e:
    print(f'Error: {e}')

# Check conversation_history
print('\n=== conversation_history ===')
try:
    for r in conn.execute('SELECT * FROM conversation_history LIMIT 10').fetchall():
        print(dict(r))
except Exception as e:
    print(f'Error: {e}')

conn.close()
