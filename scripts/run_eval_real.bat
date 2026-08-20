@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

echo ============================================================
echo  RAG Eval - plain vs agentic  (full run)
echo ============================================================

if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
if exist "venv\Scripts\activate.bat"  call "venv\Scripts\activate.bat"

REM force real vector backend; watch for "falling back to TF-IDF" in the log
set RAG_BACKEND=pgvector
set JUDGE_MAX_TOKENS=1600

REM Thinking/reasoning is auto-disabled (script negotiates the right param).
REM If auto-detect fails, uncomment ONE of these to force it:
REM set NO_THINK_PARAMS={"chat_template_kwargs":{"enable_thinking":false}}
REM set NO_THINK_PARAMS={"enable_thinking":false}
REM set NO_THINK_PARAMS={"reasoning_effort":"none"}

echo.
echo [1/3] Judge preflight + 3-case smoke run
echo ------------------------------------------------------------
python scripts\eval_real.py --mode both --limit 3 --judge-max-tokens %JUDGE_MAX_TOKENS%
if errorlevel 1 (
  echo.
  echo [ABORT] smoke run failed. Fix the judge first:
  echo   - force no-think:  set NO_THINK_PARAMS=... ^(see lines above^)
  echo   - raise budget:    --judge-max-tokens 3000
  echo   - or switch judge: --judge-model ^<non-reasoning-model^>
  pause
  exit /b 1
)

echo.
echo [2/3] Check the smoke output above - all three must hold:
echo    * "[no-think] ..." line appeared  (thinking disabled)
echo    * "[preflight] Judge JSON parse OK"
echo    * NO "pgvector failed ... falling back to TF-IDF"
echo.
set /p GO=Type Y to start the FULL run (92 cases): 
if /i not "%GO%"=="Y" (
  echo cancelled.
  pause
  exit /b 0
)

echo.
echo [3/3] Full run - results -> eval_real_result.csv
echo ------------------------------------------------------------
python scripts\eval_real.py ^
  --dataset eval\rag_eval_hard.jsonl ^
  --mode both ^
  --backend pgvector ^
  --k 5 ^
  --judge-max-tokens %JUDGE_MAX_TOKENS% ^
  --csv eval_real_result.csv

echo.
echo Done. Per-case results: eval_real_result.csv
pause
