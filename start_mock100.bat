@echo off
cd /d C:\Users\Administrator\.openclaw\workspace\langgraph-customer-service-agent
set MOCK_LLM=1
set WORKERS=100
set MOCK_LLM_DELAY_MS=50
set MOCK_LLM_TOKENS=32
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe app_fastapi.py
pause
