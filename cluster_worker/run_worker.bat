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

REM Parse command line arguments:
REM Usage options:
REM   run_worker.bat                        (uses saved settings or prompts)
REM   run_worker.bat 0                      (sets idle minutes to 0 for dedicated mode)
REM   run_worker.bat 0 Worker-2             (sets idle 0 and node ID Worker-2)
REM   run_worker.bat http://server:8000     (sets server URL)
REM   run_worker.bat http://server:8000 0   (sets server URL and idle minutes)
REM   run_worker.bat http://server:8000 0 Worker-2

set ARG1=%~1
set ARG2=%~2
set ARG3=%~3

REM Detect if ARG1 is a server URL (contains "://")
echo %ARG1% | findstr /i "://" >nul 2>&1
if %errorlevel% equ 0 (
    set SERVER_URL=%ARG1%
    echo %ARG1%> server_url.txt
    if not "%ARG2%"=="" (
        set IDLE_MINUTES=%ARG2%
        echo %ARG2%> idle_minutes.txt
    )
    if not "%ARG3%"=="" (
        set NODE_ID=%ARG3%
    )
) else (
    REM ARG1 is not a URL; if provided, it is IDLE_MINUTES
    if not "%ARG1%"=="" (
        set IDLE_MINUTES=%ARG1%
        echo %ARG1%> idle_minutes.txt
    )
    if not "%ARG2%"=="" (
        set NODE_ID=%ARG2%
    )
)

if "%SERVER_URL%"=="" (
    if exist "server_url.txt" (
        set /p SERVER_URL=<server_url.txt
    )
)
if "%IDLE_MINUTES%"=="" (
    if exist "idle_minutes.txt" (
        set /p IDLE_MINUTES=<idle_minutes.txt
    )
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

set IDLE_PARAM=
if not "%IDLE_MINUTES%"=="" (
    set IDLE_PARAM=--idle-minutes %IDLE_MINUTES%
)

set NODE_PARAM=
if not "%NODE_ID%"=="" (
    set NODE_PARAM=--node-id %NODE_ID% --name %NODE_ID%
)

echo.
if not "%SERVER_URL%"=="" (
    echo ========================================================
    echo Starting worker in PULL (Outbound) Mode
    echo Connecting to Server: %SERVER_URL%
    if "%NODE_ID%"=="" (
        echo * Worker Node ID:   Auto (Hostname)
    ) else (
        echo * Worker Node ID:   %NODE_ID%
    )
    if "%IDLE_MINUTES%"=="" (
        echo * Idle Requirement: Dynamic (Synchronized with Server)
    ) else (
        echo * Idle Requirement: %IDLE_MINUTES% min (0 = Dedicated Compute Mode)
    )
    echo * Outbound only: Bypasses hospital inbound firewalls
    echo ========================================================
    echo.
    python vpsqa_worker.py --server-url "%SERVER_URL%" --mcsquare-dir "%MCSQUARE_DIR%" %IDLE_PARAM% %NODE_PARAM%
) else (
    echo ========================================================
    echo Starting worker in PUSH Mode on port 8001
    echo ========================================================
    echo.
    python vpsqa_worker.py --port 8001 --mcsquare-dir "%MCSQUARE_DIR%" %IDLE_PARAM% %NODE_PARAM%
)

pause
