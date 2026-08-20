@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
timeout /t 1 /nobreak >nul
call .venv\Scripts\activate.bat
uvicorn app_fastapi:app --host 127.0.0.1 --port 7860
pause
