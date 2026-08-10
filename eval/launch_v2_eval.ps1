# Detached launcher for the 85-item v2 full eval run.
# Run via scheduled task so it survives OpenClaw exec-session cleanup.
# 严格模式: RAG_STRICT=1(禁用静默 TF-IDF 回落) + RAG_SEARCH_CACHE_TTL=0(禁用检索缓存)
#   —— 与 run_real_eval.py main() 的强制严格一致,这里显式设置以便日志可查。
$ErrorActionPreference = "Continue"
Set-Location "C:\Users\Administrator\.openclaw\workspace1\langgraph-customer-service-agent"
$env:RAG_STRICT = "1"
$env:RAG_SEARCH_CACHE_TTL = "0"
$log = "eval\reports\v2_full_run3.log"
"=== launch $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') RAG_STRICT=$env:RAG_STRICT TTL=$env:RAG_SEARCH_CACHE_TTL ===" | Out-File -FilePath $log -Encoding utf8
& ".\.venv\Scripts\python.exe" -u -X utf8 eval\run_real_eval.py --dataset eval\golden_set_v2.jsonl --all --multi-turn *>> $log
"=== exit code: $LASTEXITCODE @ $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $log -Append -Encoding utf8
