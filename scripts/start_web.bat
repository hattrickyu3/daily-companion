@echo off
rem Start the daily-companion web app. Double-click to run.
rem chcp 65001 keeps Japanese console output from turning into mojibake.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
uv run python -m companion.web.serve
pause
