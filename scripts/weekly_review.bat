@echo off
rem Weekly review. Registered with Task Scheduler.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
uv run python -m companion.jobs.weekly_review
