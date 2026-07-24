@echo off
REM Launcher for the Forex-EA trading loop, run inside the INTERACTIVE desktop
REM session (via the ForexEA-Bot scheduled task). MT5 only initialises reliably
REM in an interactive session, which is why this does NOT run as a session-0
REM Windows service. See deploy/README.md and CLAUDE.md.
cd /d C:\forex-ea
venv\Scripts\python.exe main.py >> logs\bot-task.log 2>&1
