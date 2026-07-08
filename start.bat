@echo off
REM Quick launch for MSFS Tablet Tracker (Windows)
cd /d "%~dp0"
if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate
REM Check that dependencies import fine, otherwise (re)install
python -c "import aiohttp, dotenv, qrcode" 2>nul || pip install -r requirements.txt
python backend\server.py
pause
