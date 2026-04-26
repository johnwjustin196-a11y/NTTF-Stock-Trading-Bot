@echo off
setlocal
cd /d "%~dp0"

set PYTHON=C:\Users\lordo\AppData\Local\Python\pythoncore-3.14-64\python.exe
if not exist "%PYTHON%" set PYTHON=python

"%PYTHON%" scripts\sync_alpaca.py

echo.
pause
