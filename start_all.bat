@echo off
setlocal
REM ================================================================
REM  One-click launcher: CS-Agent (langgraph) + SimpleAgent
REM  Usage:
REM    start_all.bat          start all services
REM    start_all.bat check    run offline diagnostics first, then ask
REM  Ports: CS backend 7860 / CS frontend 5173 / SimpleAgent 8000
REM  Prereq: local LLM server (llama.cpp :8080) running, .env configured
REM ================================================================

set LG=C:\Users\Administrator\.openclaw\workspace1\langgraph-customer-service-agent
set SA=C:\Users\Administrator\.openclaw\workspace\simple-agent

if /i "%1"=="check" (
    echo ============ Diagnose: CS-Agent ============
    cd /d %LG%
    call .venv\Scripts\activate.bat
    python scripts\diagnose.py
    call .venv\Scripts\deactivate.bat 2>nul
    echo.
    echo ============ Diagnose: SimpleAgent ============
    cd /d %SA%
    python scripts\diagnose.py
    echo.
    set /p GOON=Continue to start all services? [Y/n]
    if /i "%GOON%"=="n" exit /b 0
)

echo [1/4] Starting CS-Agent backend on :7860 ...
cd /d %LG%
if not defined CS_WORKERS set "CS_WORKERS=2"
set "PROMETHEUS_MULTIPROC_DIR=%TEMP%\langgraph-prometheus-%RANDOM%-%RANDOM%"
if not exist "%PROMETHEUS_MULTIPROC_DIR%" mkdir "%PROMETHEUS_MULTIPROC_DIR%"
start "CS-Backend 7860" cmd /k ".venv\Scripts\activate.bat && set PORT=7860&& set PROMETHEUS_MULTIPROC_DIR=%PROMETHEUS_MULTIPROC_DIR%&& uvicorn app_fastapi:app --host 127.0.0.1 --port 7860 --workers %CS_WORKERS% --no-access-log"

echo [2/4] Starting SimpleAgent on :8000 ...
cd /d %SA%
start "SimpleAgent 8000" cmd /k "python app_prod.py"

echo [3/4] Starting CS-Agent frontend on :5173 ...
cd /d %LG%\frontend
start "CS-Frontend 5173" cmd /k "set BACKEND_URL=http://127.0.0.1:7860&& npm run dev"

echo [4/4] Waiting, then opening browsers ...
timeout /t 8 /nobreak >nul
start http://localhost:5173
start http://localhost:8000

echo.
echo   CS frontend   http://localhost:5173
echo   CS backend    http://127.0.0.1:7860   (online check: python scripts\diagnose.py --server http://127.0.0.1:7860)
echo   SimpleAgent   http://localhost:8000   (online check: python scripts\diagnose.py --server http://127.0.0.1:8000)
echo.
echo   To stop: close the three cmd windows.
endlocal
