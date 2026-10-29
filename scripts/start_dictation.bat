@echo off
rem Start the dictation tool (tap Right Ctrl, speak, tap again to type at the cursor).
rem Double-click to start it now. It also starts at logon once register_tasks.ps1 has been run.
rem pythonw runs it without a console window. Running it twice is safe (the second one exits).
cd /d "%~dp0.."
start "" ".venv\Scripts\pythonw.exe" -m companion.dictation
