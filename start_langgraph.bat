@echo off
setlocal
REM One-click: CS-Agent backend (7860) + frontend (5173)
REM Prereq: local LLM server running, .env configured
cd /d %~dp0
call .venv\Scripts\activate.bat
start "CS-Backend 7860" cmd /k ".venv\Scripts\activate.bat && set PORT=7860&& uvicorn app_fastapi:app --host 127.0.0.1 --port 7860"
cd frontend
start "CS-Frontend 5173" cmd /k "set BACKEND_URL=http://127.0.0.1:7860&& npm run dev"
timeout /t 6 /nobreak >nul
start http://localhost:5173
echo Backend http://127.0.0.1:7860 / Frontend http://localhost:5173
endlocal
