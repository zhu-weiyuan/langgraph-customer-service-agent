# Detached launcher for the 85-item v2 full eval run.
# Run via scheduled task so it survives OpenClaw exec-session cleanup.
$ErrorActionPreference = "Continue"
Set-Location "C:\Users\Administrator\.openclaw\workspace1\langgraph-customer-service-agent"
$log = "eval\reports\v2_full_run2.log"
"=== launch $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $log -Encoding utf8
& ".\.venv\Scripts\python.exe" -u -X utf8 eval\run_real_eval.py --dataset eval\golden_set_v2.jsonl --all *>> $log
"=== exit code: $LASTEXITCODE @ $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $log -Append -Encoding utf8
