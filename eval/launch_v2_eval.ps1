# Detached launcher for the 85-item v2 full eval run.
# Run via scheduled task so it survives OpenClaw exec-session cleanup.
# 严格模式: RAG_STRICT=1(禁用静默 TF-IDF 回落) + RAG_SEARCH_CACHE_TTL=0(禁用检索缓存)
# 日志用 cmd 重定向(原始字节),避免 PowerShell 管道把 python -X utf8 输出重编码成 UTF-16 乱码。
$ErrorActionPreference = "Continue"
Set-Location "C:\Users\Administrator\.openclaw\workspace1\langgraph-customer-service-agent"
$env:RAG_STRICT = "1"
$env:RAG_SEARCH_CACHE_TTL = "0"
$log = "eval\reports\v2_full_run3.log"
cmd /c "echo === launch %date% %time% RAG_STRICT=%RAG_STRICT% TTL=%RAG_SEARCH_CACHE_TTL% === >> $log 2>&1 && .venv\Scripts\python.exe -u -X utf8 eval\run_real_eval.py --dataset eval\golden_set_v2.jsonl --all --multi-turn >> $log 2>&1 & echo === exit code: %errorlevel% @ %date% %time% === >> $log 2>&1"
