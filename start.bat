@echo off
cd /d "%~dp0"
taskkill /F /IM python.exe 2>nul
timeout /t 2 /nobreak >nul
python run.py
