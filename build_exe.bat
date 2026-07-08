@echo off
REM ============================================================
REM  Build the standalone executable (distribution)
REM  Output: dist\MSFS-Tablet-Tracker.exe
REM  Distribute it as-is: no Python required on user machines.
REM ============================================================
cd /d "%~dp0"

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate
python -c "import aiohttp, dotenv, qrcode" 2>nul || pip install -r requirements.txt
pip install pyinstaller

pyinstaller --onefile --clean --name MSFS-Tablet-Tracker ^
    --add-data "frontend;frontend" ^
    --collect-all SimConnect ^
    backend\server.py

echo.
echo ============================================================
echo  Done! Executable: dist\MSFS-Tablet-Tracker.exe
echo  Distribute this single file (optionally with .env.example).
echo  On first launch it creates data\ and .env next to itself.
echo ============================================================
pause
