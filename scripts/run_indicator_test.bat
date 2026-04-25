@echo off
setlocal
cd /d "%~dp0.."

echo ============================================================
echo   Indicator Backtester
echo.
echo   Tests a single indicator in isolation using real 15-min
echo   candle data. Signal logic mirrors the live entry queue
echo   exactly. Exit logic uses the ratcheting locked-profit stop.
echo.
echo   Results saved to: data\indicator_tests\
echo.
echo   Options:
echo     --indicator fib         Which indicator (currently: fib)
echo     --days N                Trading days to test (default 180)
echo     --tickers AMD NVDA ...  Specific tickers (default: watchlist)
echo     --tp 0.05               Take-profit trigger (default: settings.yaml)
echo     --sl 0.02               Min stop distance (default: settings.yaml)
echo     --sl-max 0.05           Max stop distance (default: settings.yaml)
echo     --max-hold 5            Max days to hold (default: 5)
echo.
echo   Examples:
echo     run_indicator_test.bat
echo     run_indicator_test.bat --days 90
echo     run_indicator_test.bat --tickers AMD NVDA AAPL --days 120
echo     run_indicator_test.bat --tp 0.07 --sl 0.025 --days 180
echo ============================================================
echo.

set PYTHON=C:\Users\lordo\AppData\Local\Python\pythoncore-3.14-64\python.exe
if not exist "%PYTHON%" set PYTHON=python

"%PYTHON%" scripts\test_indicator.py --indicator fib %*

echo.
pause
