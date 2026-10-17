@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

title QuotTracker
echo.
echo  QuotTracker - Local launcher
echo  ===========================
echo.
echo  Initializing the database schema (no sample data)...
python setup_db.py
if errorlevel 1 (
    echo Database initialization failed.
    pause
    exit /b 1
)

echo.
echo  Starting the application at http://127.0.0.1:8000
echo  Press Ctrl+C to stop.
echo.
start "" http://127.0.0.1:8000
python -m uvicorn api:app --host 127.0.0.1 --port 8000
pause
