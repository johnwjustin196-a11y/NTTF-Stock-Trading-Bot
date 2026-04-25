@echo off
echo ==== Restart trading bot ====
echo.
echo [1/3] Killing running python.exe process trees...
taskkill /F /IM python.exe /T
echo.
echo [2/3] Waiting 3 seconds for cleanup...
timeout /t 3 /nobreak >nul
echo.
echo [3/3] Launching start-bot.bat...
start "" "%~dp0start-bot.bat"
echo.
echo ==== DONE — bot relaunched in a new window ====
timeout /t 3 /nobreak >nul
