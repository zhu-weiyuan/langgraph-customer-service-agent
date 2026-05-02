@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
timeout /t 1 /nobreak >nul
python app.py
pause
