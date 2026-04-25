@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Trading Bot Dashboard
echo ============================================================
echo.

echo Your browser will open automatically.
echo Press Ctrl+C in this window to stop the dashboard.
echo.

set PYTHON=C:\Users\lordo\AppData\Local\Python\pythoncore-3.14-64\python.exe
if not exist "%PYTHON%" set PYTHON=python
"%PYTHON%" -m streamlit run dashboard.py

echo.
echo ============================================================
echo   Dashboard stopped.
echo ============================================================
pause
