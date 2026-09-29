@echo off
rem Re-run the summarization for diary entries that have none.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
uv run python -m companion.jobs.restructure
pause
