"""Check the database for sessions and user data."""
import sqlite3

db_path = 'C:/Users/Administrator/.openclaw/workspace/langgraph-customer-service-agent/user_memory.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# Check tables
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('Tables:', [r[0] for r in tables])

# Check sessions table
try:
    rows = conn.execute('SELECT * FROM sessions ORDER BY last_active DESC LIMIT 10').fetchall()
    print(f'\nSessions count: {len(rows)}')
    for r in rows:
        print(dict(r))
except Exception as e:
    print(f'Sessions error: {e}')

# Check user_ids in sessions
try:
    rows = conn.execute('SELECT DISTINCT user_id, COUNT(*) as cnt FROM sessions GROUP BY user_id').fetchall()
    print(f'\nUser IDs in sessions:')
    for r in rows:
        print(dict(r))
except Exception as e:
    print(f'User IDs error: {e}')

# Check messages count
try:
    # Try both tables
    for tbl in ['messages', 'conversations', 'message_history']:
        try:
            cnt = conn.execute(f'SELECT COUNT(*) as c FROM {tbl}').fetchone()
            print(f'Total in {tbl}: {cnt["c"]}')
        except:
            pass
except Exception as e:
    print(f'Messages error: {e}')

# Check trace.db for session info
print('\n=== Checking trace.db ===')
try:
    import os
    trace_path = 'C:/Users/Administrator/.openclaw/workspace/langgraph-customer-service-agent/data/trace.db'
    if os.path.exists(trace_path):
        tconn = sqlite3.connect(trace_path)
        tconn.row_factory = sqlite3.Row
        ttables = tconn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        print(f'trace.db tables: {[r[0] for r in ttables]}')
        # Check traces table
        for tbl in ['traces', 'sessions', 'messages', 'trace_sessions']:
            try:
                rows = tconn.execute(f'SELECT * FROM {tbl} LIMIT 3').fetchall()
                print(f'{tbl}: {len(rows)} rows')
                for r in rows:
                    print(f'  {dict(r)}')
            except Exception as e2:
                print(f'{tbl}: {e2}')
        tconn.close()
except Exception as e:
    print(f'trace.db error: {e}')

conn.close()
print('\nDone.')
