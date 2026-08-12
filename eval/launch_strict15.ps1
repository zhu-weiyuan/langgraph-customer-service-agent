# 严谨评测 launcher：15 条混合(含5条长对话) × 1 次，schtasks 脱管运行
# 目的：换回 27B 后的严谨基线（不针对测试集优化，不加 rewrite）
$ErrorActionPreference = "Continue"
Set-Location "C:\Users\Administrator\.openclaw\workspace1\langgraph-customer-service-agent"
$env:RAG_STRICT = "1"
$env:RAG_SEARCH_CACHE_TTL = "0"
$log = "eval\reports\strict15_run.log"
$ids = "online-failure-05,high-frequency-04,multi-hop-02,business-critical-10,business-critical-07,online-failure-08,online-failure-04,online-failure-02,exact-match-11,high-frequency-05,long-conv-memory-01,long-conv-memory-02,long-conv-memory-03,long-conv-switch-04,long-conv-refusal-05"
cmd /c "echo === launch %date% %time% === >> $log 2>&1 && .venv\Scripts\python.exe -u -X utf8 eval\run_real_eval.py --ids $ids --multi-turn >> $log 2>&1 & echo === exit %errorlevel% @ %date% %time% === >> $log 2>&1"
