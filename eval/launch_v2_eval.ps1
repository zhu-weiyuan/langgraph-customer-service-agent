# Detached launcher for the v2 full evaluation run.
# Run via scheduled task so it survives the interactive terminal closing.
# Strict mode: pgvector failure is reported as an evaluation error; no silent TF-IDF fallback.
# Cache is disabled so repeated/similar queries do not reuse stale retrieval results.
$ErrorActionPreference = "Continue"
Set-Location "C:\Users\Administrator\.openclaw\workspace1\langgraph-customer-service-agent"
$env:RAG_STRICT = "1"
$env:RAG_SEARCH_CACHE_TTL = "0"
$log = "eval\reports\v2_full_run3.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -LiteralPath $log -Value "=== launch $stamp RAG_STRICT=$env:RAG_STRICT TTL=$env:RAG_SEARCH_CACHE_TTL ==="
# cmd /v:on lets us capture the real Python exit code after the process finishes.
cmd /v:on /c ".venv\Scripts\python.exe -u -X utf8 eval\run_real_eval.py --dataset eval\golden_set_v2.jsonl --all --multi-turn >> $log 2>&1 & set code=!errorlevel! & echo === exit code: !code! @ %date% %time% === >> $log 2>&1 & exit /b !code!"
exit $LASTEXITCODE
