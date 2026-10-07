@echo off
REM Starts the tunnel, points Telnyx at it, and runs the backend + dashboard.
cd /d "%~dp0"
if exist backend\venv\Scripts\activate.bat (
    call backend\venv\Scripts\activate.bat
) else if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
) else (
    echo Run setup.bat first to create the Python environment.
    pause
    exit /b 1
)
python start.py
pause
