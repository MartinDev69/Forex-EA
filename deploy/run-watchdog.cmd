@echo off
REM Watchdog tick wrapper. cd into the repo first so watchdog.py's relative
REM paths (data/trades.db, logs/) resolve regardless of the scheduler's
REM default working directory (SYSTEM tasks otherwise start in System32).
cd /d C:\forex-ea
venv\Scripts\python.exe scripts\watchdog.py >> logs\watchdog.log 2>&1
