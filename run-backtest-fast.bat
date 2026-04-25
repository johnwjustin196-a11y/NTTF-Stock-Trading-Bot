@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Stock Bot Fast Backtester (no LLM, with cached deep scores)
echo.
echo   Runs using technical, news, breadth, and deep scorer cache.
echo   No live LLM calls. Safe to run alongside the live bot.
echo   Requires backfill cache: run scripts\run_backfill.bat first.
echo.
echo   Completes in under 30 minutes for 90 days.
echo   Results saved to: data\backtest_results.json
echo.
echo   Extra flags you can add after pressing Enter on the prompt:
echo     --cash N        Starting capital (default 100000)
echo     --skip-days N   Resume: skip first N days
echo     TICK1 TICK2 ... Specific tickers instead of shortlist
echo ============================================================
echo.

set DAYS=90
set /p DAYS=Enter number of backtest days [default 90]:

if "%DAYS%"=="" set DAYS=90

echo.
echo Running %DAYS%-day fast backtest (no LLM)...
echo.

set BOT_LOG_FILE=logs/backtest.log
set PYTHON=C:\Users\lordo\AppData\Local\Python\pythoncore-3.14-64\python.exe
if not exist "%PYTHON%" set PYTHON=python
"%PYTHON%" -m src.backtester.engine --no-llm --days %DAYS% %*

echo.
echo Fast backtest complete. Results saved to data\backtest_results.json
pause
