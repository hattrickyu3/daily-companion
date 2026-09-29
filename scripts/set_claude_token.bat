@echo off
rem Write the long-lived Claude token from the clipboard into .env, then restart the web app.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set_claude_token.ps1"
pause
