@echo off
REM ============================================================
REM  Medicare VoiceOps - one-time setup (Windows)
REM  Needs: Python 3.11 or 3.12, and Node.js 20+ installed.
REM ============================================================
cd /d "%~dp0"

echo.
echo [1/3] Creating Python environment...
cd backend
if not exist venv (
    py -3.11 -m venv venv 2>nul || py -3.12 -m venv venv 2>nul || python -m venv venv
)
call venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
echo [2/3] Installing backend packages (this takes a few minutes the first time)...
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Backend install failed. Scroll up for the error.
    pause
    exit /b 1
)
echo Downloading speech text data...
python -m nltk.downloader punkt_tab
cd ..

echo.
echo [3/3] Building the dashboard...
cd frontend
call npm install
call npm run build
if errorlevel 1 (
    echo.
    echo Dashboard build failed. Is Node.js installed? https://nodejs.org
    pause
    exit /b 1
)
cd ..

echo.
echo ============================================================
echo  Setup complete.
echo  Next: run the database script (see README step 2), then
echo  double-click start.bat
echo ============================================================
pause
