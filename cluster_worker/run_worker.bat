@echo off
title Virtual PSQA - Idle MCsquare Worker
echo ========================================================
echo   Virtual PSQA - Distributed Monte Carlo Worker
echo ========================================================
echo.

set MCSQUARE_DIR=..\MCsquare
if not exist "%MCSQUARE_DIR%" (
    set MCSQUARE_DIR=.\MCsquare
)

echo Checking dependencies...
python -c "import httpx, numpy" 2>nul
if %errorlevel% neq 0 (
    echo Installing worker dependencies (httpx, numpy, fastapi, uvicorn)...
    python -m pip install -r requirements.txt
)

REM Check for saved server URL
if exist "server_url.txt" (
    set /p SERVER_URL=<server_url.txt
)

if "%SERVER_URL%"=="" (
    echo.
    echo Enter the Virtual PSQA Server address you use in your browser.
    echo (e.g. http://172.20.145.65:8000 or http://172.20.145.65:8080)
    echo.
    set /p USER_INPUT="Server URL [press Enter to skip for push mode]: "
    if not "%USER_INPUT%"=="" (
        set SERVER_URL=%USER_INPUT%
        echo %USER_INPUT%> server_url.txt
    )
)

echo.
if not "%SERVER_URL%"=="" (
    echo ========================================================
    echo Starting worker in PULL (Outbound) Mode
    echo Connecting to Server: %SERVER_URL%
    echo * Outbound only: Bypasses hospital inbound firewalls
    echo * Automatic user activity detection and CPU yielding
    echo ========================================================
    echo.
    python vpsqa_worker.py --server-url "%SERVER_URL%" --mcsquare-dir "%MCSQUARE_DIR%" --idle-minutes 5.0 --max-cpu-pct 30.0
) else (
    echo ========================================================
    echo Starting worker in PUSH Mode on port 8001
    echo ========================================================
    echo.
    python vpsqa_worker.py --port 8001 --mcsquare-dir "%MCSQUARE_DIR%" --idle-minutes 5.0 --max-cpu-pct 30.0
)

pause
