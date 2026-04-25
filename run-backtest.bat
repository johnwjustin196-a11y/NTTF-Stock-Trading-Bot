@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Stock Bot Walk-Forward Backtester
echo.
echo   Replays trading days one at a time using only data that
echo   would have been available at each point in time.
echo.
echo   What it uses at each simulation date:
echo     - Price/technical data sliced to that date (no lookahead)
echo     - NewsAPI headlines filtered to that date range
echo     - Deep scorer (5-dimension) run every Monday
echo     - LLM with temporal guard: "Analyze as of YYYY-MM-DD"
echo.
echo   Results saved to: data\backtest_results.json
echo.
echo   Extra flags you can add after pressing Enter on the prompt:
echo     --no-llm        Skip LLM calls (faster, less accurate)
echo     --no-deep       Skip deep scorer (much faster)
echo     --cash N        Starting capital (default 100000)
echo     --skip-days N   Resume: skip first N days
echo     TICK1 TICK2 ... Specific tickers instead of shortlist
echo ============================================================
echo.

set DAYS=90
set /p DAYS=Enter number of backtest days [default 90]:

if "%DAYS%"=="" set DAYS=90

echo.
echo Running %DAYS%-day backtest...
echo.

set BOT_LOG_FILE=logs/backtest.log
set PYTHON=C:\Users\lordo\AppData\Local\Python\pythoncore-3.14-64\python.exe
if not exist "%PYTHON%" set PYTHON=python
"%PYTHON%" -m src.backtester.engine --days %DAYS% %*

echo.
echo Backtest complete. Results saved to data\backtest_results.json
pause
