@echo off
REM Starts the tunnel, points Telnyx at it, and runs the backend + dashboard.
cd /d "%~dp0backend"
if not exist venv (
    echo Run setup.bat first.
    pause
    exit /b 1
)
call venv\Scripts\activate.bat
python start.py
pause
