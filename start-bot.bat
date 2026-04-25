@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Stock Trading Bot - scheduler running (PAPER MODE)
echo.
echo   The bot will stay idle until market hours (Eastern Time):
echo     04:30 AM - pre-market scan
echo     09:30 AM - trade decisions
echo     12:00 PM - trade decisions
echo     02:00 PM - trade decisions
echo     04:30 PM - end-of-day review
echo.
echo   Close this window (or press Ctrl+C) to stop the bot.
echo   Your computer must stay awake for scheduled jobs to run.
echo ============================================================
echo.

set PYTHON=C:\Users\lordo\AppData\Local\Python\pythoncore-3.14-64\python.exe
if not exist "%PYTHON%" set PYTHON=python
"%PYTHON%" -m src.main

echo.
echo Bot stopped.
pause
