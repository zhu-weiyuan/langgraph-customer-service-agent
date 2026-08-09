@echo off
REM ===================================================================
REM  One-click application-layer load test (Windows)
REM
REM  What it does:
REM    1. sets MOCK_LLM=1 / MOCK_EMBEDDING=1  (no real LLM calls)
REM    2. starts uvicorn with %WORKERS% worker processes
REM    3. waits until /healthz answers
REM    4. runs scripts\loadtest\run_loadtest.py
REM    5. writes reports\loadtest_<users>u.json / .csv
REM    6. shuts the server down
REM
REM  Usage:
REM    scripts\loadtest\run_loadtest.bat
REM    scripts\loadtest\run_loadtest.bat 100 60 4
REM                                      ^users ^duration ^workers
REM
REM  Env overrides (set before calling):
REM    WORKERS, USERS, DURATION, RAMP, PORT, MOCK_LLM_DELAY_MS,
REM    LOADTEST_API_KEY, PYTHON
REM ===================================================================

setlocal EnableDelayedExpansion

REM ---- project root = two levels up from this file -------------------
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%..\.." || exit /b 1
set "ROOT=%CD%"

REM ---- parameters ---------------------------------------------------
if not "%~1"=="" set "USERS=%~1"
if not "%~2"=="" set "DURATION=%~2"
if not "%~3"=="" set "WORKERS=%~3"

if "%USERS%"==""    set "USERS=100"
if "%DURATION%"=="" set "DURATION=60"
if "%WORKERS%"==""  set "WORKERS=4"
if "%RAMP%"==""     set "RAMP=15"
if "%PORT%"==""     set "PORT=7860"
if "%PYTHON%"==""   set "PYTHON=python"
if "%MOCK_LLM_DELAY_MS%"=="" set "MOCK_LLM_DELAY_MS=200"

REM ---- mock switches: LLM and embeddings never leave the process -----
set "MOCK_LLM=1"
set "MOCK_EMBEDDING=1"
set "MOCK_LLM_JSON_DELAY_MS=%MOCK_LLM_DELAY_MS%"
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

set "STAMP=%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%_%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%"
set "STAMP=%STAMP: =0%"
set "REPORT_DIR=%ROOT%\reports"
if not exist "%REPORT_DIR%" mkdir "%REPORT_DIR%"
set "JSON_OUT=%REPORT_DIR%\loadtest_%USERS%u_%WORKERS%w_%STAMP%.json"
set "CSV_OUT=%REPORT_DIR%\loadtest_%USERS%u_%WORKERS%w_%STAMP%.csv"
set "SERVER_LOG=%REPORT_DIR%\uvicorn_%STAMP%.log"

echo ===================================================================
echo  Application-layer load test  (LLM is mocked - see LOADTEST_README)
echo ===================================================================
echo   root      : %ROOT%
echo   workers   : %WORKERS%
echo   users     : %USERS%
echo   duration  : %DURATION%s   ramp: %RAMP%s
echo   mock delay: %MOCK_LLM_DELAY_MS% ms per LLM call
echo   port      : %PORT%
echo   report    : %JSON_OUT%
echo.

REM ---- start uvicorn in a separate window ---------------------------
echo [1/4] starting uvicorn with %WORKERS% workers ...
start "loadtest-uvicorn" /MIN cmd /c "%PYTHON% -m uvicorn app_fastapi:app --host 127.0.0.1 --port %PORT% --workers %WORKERS% --log-level warning > "%SERVER_LOG%" 2>&1"

REM ---- wait for readiness (max 60 s) --------------------------------
echo [2/4] waiting for http://127.0.0.1:%PORT%/healthz ...
set "READY="
for /L %%i in (1,1,60) do (
    if not defined READY (
        %PYTHON% -c "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%PORT%/healthz',timeout=2).status==200 else 1)" >nul 2>&1
        if !ERRORLEVEL!==0 (
            set "READY=1"
            echo       ready after %%i s
        ) else (
            timeout /t 1 /nobreak >nul
        )
    )
)
if not defined READY (
    echo [ERROR] server did not become ready in 60s. See %SERVER_LOG%
    call :SHUTDOWN
    popd
    exit /b 1
)

REM ---- run the load test --------------------------------------------
echo [3/4] running load test ...
echo.
%PYTHON% "%ROOT%\scripts\loadtest\run_loadtest.py" ^
    --host http://127.0.0.1:%PORT% ^
    --users %USERS% --duration %DURATION% --ramp %RAMP% ^
    --profile --proc-filter uvicorn ^
    --json "%JSON_OUT%" --csv "%CSV_OUT%" ^
    --label "workers=%WORKERS%,mock_delay=%MOCK_LLM_DELAY_MS%ms"
set "RC=%ERRORLEVEL%"

REM ---- shut down ------------------------------------------------------
echo.
echo [4/4] stopping server ...
call :SHUTDOWN

echo.
echo Reports:
echo   %JSON_OUT%
echo   %CSV_OUT%
echo   %SERVER_LOG%   (server log)
echo.
echo Remember: these numbers are APPLICATION-layer numbers (LLM mocked at
echo %MOCK_LLM_DELAY_MS% ms). They do not describe real end-user latency
echo with the 35B model. See scripts\loadtest\LOADTEST_README.md.

popd
exit /b %RC%

REM ===================================================================
:SHUTDOWN
REM kill every python process that is serving this port
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:"LISTENING" ^| findstr ":%PORT% "') do (
    taskkill /PID %%p /T /F >nul 2>&1
)
timeout /t 2 /nobreak >nul
exit /b 0
