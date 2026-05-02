@echo off
cd /d "%~dp0"
timeout /t 1 /nobreak >nul
python app.py
