@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Stock Bot - Today's Session Backtest
echo.
echo   Runs a 1-day backtest using real market data from today.
echo   Use this AFTER market close (4:30 PM ET or later).
echo.
echo   Runs exactly like the live bot would have run today:
echo     - 6 decision cycles (09:30, 11:30, 12:30, 13:30, 14:30, 15:30)
echo     - Circuit breaker, trailing stops, all pre-entry filters
echo     - LLM advisor + deep scorer
echo     - Same signals and thresholds as live
echo.
echo   Results saved to: data\backtest_results.json
echo ============================================================
echo.

set PYTHON=C:\Users\lordo\AppData\Local\Python\pythoncore-3.14-64\python.exe
if not exist "%PYTHON%" set PYTHON=python
"%PYTHON%" -m src.backtester.engine --today %*

echo.
echo Today's backtest complete. Results saved to data\backtest_results.json
pause
