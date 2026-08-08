@echo off
rem Nightly diary reminder. Registered with Task Scheduler in a later phase.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
uv run python -m companion.jobs.evening_prompt
