@echo off
:: Double-click this file on Windows to launch Research Task Manager.
cd /d "%~dp0"

echo ========================================
echo   Research Task Manager
echo ========================================

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo         Install from https://python.org and try again.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [->] Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate
echo [->] Checking dependencies...
pip install -q -r requirements.txt

echo [->] Starting app at http://localhost:8080
echo      Close this window to stop.
echo.
python app.py
pause
