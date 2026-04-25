@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Stock Deep Scorer - 5-dimension research grader
echo.
echo   Scores all shortlist tickers across:
echo     Technical  (25%)  - price action, indicators, trend
echo     Fundamental(25%)  - revenue, margins, valuation
echo     Sentiment  (20%)  - news, analyst ratings, short interest
echo     Risk       (15%)  - volatility, drawdown, liquidity
echo     Thesis     (15%)  - LLM synthesis and conviction
echo.
echo   Results saved to: data\trade_scores.json
echo   Valid for 7 days. Bot reads these automatically.
echo.
echo   To score specific tickers instead of the full shortlist:
echo     python -m src.analysis.deep_scorer BBAI NVDA TSLA
echo ============================================================
echo.

set PYTHON=C:\Users\lordo\AppData\Local\Python\pythoncore-3.14-64\python.exe
if not exist "%PYTHON%" set PYTHON=python

if "%~1"=="" (
    echo Scoring full shortlist...
    echo.
    "%PYTHON%" -m src.analysis.deep_scorer
) else (
    echo Scoring: %*
    echo.
    "%PYTHON%" -m src.analysis.deep_scorer %*
)

echo.
echo Deep scorer finished. Results in data\trade_scores.json
pause
