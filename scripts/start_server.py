# -*- coding: utf-8 -*-
"""Start the FastAPI server correctly with all env vars"""
import subprocess, os, time, sys

BASE = r'C:\Users\Administrator\.openclaw\workspace\langgraph-customer-service-agent'
os.chdir(BASE)

env = os.environ.copy()
env['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'
env['API_KEYS'] = 'test-key'
env['OPENAI_BASE_URL'] = 'http://localhost:8080/v1'
env['OPENAI_API_KEY'] = 'sk-local'
env['RAG_BACKEND'] = 'pgvector'
env['JWT_SECRET'] = 'test-jwt-secret'

proc = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'app_fastapi:app', '--host', '0.0.0.0', '--port', '7860', '--workers', '1'],
    cwd=BASE,
    env=env,
    stdout=open(os.path.join(BASE, 'server_stdout.log'), 'w', encoding='utf-8'),
    stderr=open(os.path.join(BASE, 'server_stderr.log'), 'w', encoding='utf-8'),
)
print('Server PID:', proc.pid)

# Wait for startup
time.sleep(8)

import urllib.request
try:
    r = urllib.request.urlopen('http://localhost:7860/healthz', timeout=5)
    print('Server OK:', r.status)
except Exception as e:
    print('Server not ready:', str(e)[:80])
    # Check stderr
    with open(os.path.join(BASE, 'server_stderr.log'), 'r', encoding='utf-8') as f:
        err = f.read()
        if err:
            print('Stderr:', err[:500])
