@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Stock Trading Bot - first-time setup
echo ============================================================
echo.

REM --- Check Python ----------------------------------------------------
set PYTHON=C:\Users\lordo\AppData\Local\Python\pythoncore-3.14-64\python.exe
if not exist "%PYTHON%" set PYTHON=python

where "%PYTHON%" >nul 2>&1
if errorlevel 1 (
    echo ERROR: 'python' was not found on PATH.
    echo.
    echo If you already installed Python, you may need to re-open this
    echo script after restarting your computer, OR re-run the Python
    echo installer and make sure the box "Add python.exe to PATH" is
    echo checked on the first screen.
    echo.
    echo Download: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo Python found:
"%PYTHON%" --version
echo.

REM --- Upgrade pip (quiet) --------------------------------------------
echo [1/3] Upgrading pip...
"%PYTHON%" -m pip install --upgrade pip >nul 2>&1

REM --- Install requirements -------------------------------------------
echo [2/3] Installing dependencies (this can take 2-5 minutes)...
"%PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: dependency install failed. Scroll up to see which package.
    echo Common fix: run this script again - sometimes PyPI is flaky.
    pause
    exit /b 1
)

REM --- Create .env if missing -----------------------------------------
echo.
echo [3/3] Setting up .env file...
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo    Created .env - you can leave it blank for sim mode.
) else (
    echo    .env already exists - leaving it alone.
)

echo.
echo ============================================================
echo   Setup complete!
echo.
echo   Next: double-click test-run.bat to watch the bot do a
echo         full simulation cycle.
echo ============================================================
echo.
pause
