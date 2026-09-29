@echo off
rem Morning briefing catch-up at boot. Registered with Task Scheduler.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
uv run python -m companion.jobs.morning_catchup
