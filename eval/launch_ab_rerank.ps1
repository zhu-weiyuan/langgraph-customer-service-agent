# Detached launcher for rerank A/B (remote vs rule, same questions).
# Run via scheduled task so it survives OpenClaw exec-session cleanup.
# 日志用 cmd 重定向(原始字节),避免 PowerShell 管道 UTF-16 重编码乱码。
$ErrorActionPreference = "Continue"
Set-Location "C:\Users\Administrator\.openclaw\workspace1\langgraph-customer-service-agent"
$env:RAG_STRICT = "1"
$env:RAG_SEARCH_CACHE_TTL = "0"
$log = "eval\reports\ab_rerank_run.log"
cmd /c "echo === launch %date% %time% === >> $log 2>&1 && .venv\Scripts\python.exe -u -X utf8 eval\ab_rerank.py --ids exact-match-02,online-failure-01,online-failure-09,business-critical-02,business-critical-07,multi-hop-03,multi-hop-06,comparison-01,high-frequency-06,privilege-expiry-06,exact-match-05,privilege-expiry-04 >> $log 2>&1 & echo === exit code: %errorlevel% @ %date% %time% === >> $log 2>&1"
