@echo off
title Virtual PSQA - Idle MCsquare Worker
echo ========================================================
echo   Virtual PSQA - Distributed Monte Carlo Worker
echo ========================================================
echo.

set MCSQUARE_DIR=..\MCsquare
if not exist "%MCSQUARE_DIR%" set MCSQUARE_DIR=.\MCsquare

echo Checking dependencies...
python -c "import httpx, numpy" 2>nul
if %errorlevel% neq 0 echo Installing worker dependencies...
if %errorlevel% neq 0 python -m pip install -r requirements.txt

REM Parse command line arguments:
REM   run_worker.bat                        (uses saved settings or prompts)
REM   run_worker.bat 0                      (sets idle minutes to 0 for dedicated mode)
REM   run_worker.bat 0 Worker-2             (sets idle 0 and node ID Worker-2)
REM   run_worker.bat http://server:8003     (sets server URL)
REM   run_worker.bat http://server:8003 0   (sets server URL and idle minutes)
REM   run_worker.bat http://server:8003 0 Worker-2
REM   run_worker.bat reset                  (clears saved server URL and settings)

set ARG1=%~1
set ARG2=%~2
set ARG3=%~3

if /i "%ARG1%"=="reset" goto reset_config
if /i "%ARG1%"=="clear" goto reset_config
if /i "%ARG1%"=="--reset" goto reset_config

REM Detect if ARG1 is a server URL (contains :// or starts with digits for an IP)
echo %ARG1% | findstr /i "://" >nul 2>&1
if %errorlevel% equ 0 goto parse_url

echo %ARG1% | findstr /r "^[0-9][0-9]*\.[0-9]" >nul 2>&1
if %errorlevel% equ 0 goto parse_ip_url

REM ARG1 is not a URL; if provided, it is IDLE_MINUTES
if not "%ARG1%"=="" set IDLE_MINUTES=%ARG1%
if not "%ARG1%"=="" > "%~dp0idle_minutes.txt" echo %IDLE_MINUTES%
if not "%ARG2%"=="" set NODE_ID=%ARG2%
goto load_saved

:reset_config
if exist "%~dp0server_url.txt" del /f /q "%~dp0server_url.txt"
if exist "%~dp0idle_minutes.txt" del /f /q "%~dp0idle_minutes.txt"
echo.
echo [INFO] Reset saved server URL and configuration.
set ARG1=
set ARG2=
set ARG3=
set SERVER_URL=
set IDLE_MINUTES=
goto prompt_url

:parse_ip_url
set ARG1=http://%ARG1%

:parse_url
set SERVER_URL=%ARG1%
> "%~dp0server_url.txt" echo %SERVER_URL%
if not "%ARG2%"=="" set IDLE_MINUTES=%ARG2%
if not "%ARG2%"=="" > "%~dp0idle_minutes.txt" echo %IDLE_MINUTES%
if not "%ARG3%"=="" set NODE_ID=%ARG3%

:load_saved
if "%SERVER_URL%"=="" if exist "%~dp0server_url.txt" set /p SERVER_URL=<"%~dp0server_url.txt"
if "%IDLE_MINUTES%"=="" if exist "%~dp0idle_minutes.txt" set /p IDLE_MINUTES=<"%~dp0idle_minutes.txt"

REM If arguments were passed on command line, skip interactive prompt and launch directly
if not "%ARG1%"=="" goto launch

:prompt_url
echo.
if not "%SERVER_URL%"=="" echo Current Server URL: %SERVER_URL%
if not "%SERVER_URL%"=="" echo Config File Path:   %~dp0server_url.txt
echo.
if not "%SERVER_URL%"=="" set /p USER_INPUT="Server URL [Press Enter to keep '%SERVER_URL%', or type new URL]: "
if "%SERVER_URL%"=="" set /p USER_INPUT="Server URL [e.g. http://172.20.145.65:8003, or Enter for push mode]: "
if "%USER_INPUT%"=="" goto launch

echo %USER_INPUT% | findstr /i "://" >nul 2>&1
if %errorlevel% neq 0 set USER_INPUT=http://%USER_INPUT%

set SERVER_URL=%USER_INPUT%
> "%~dp0server_url.txt" echo %SERVER_URL%

:launch
set IDLE_PARAM=
if not "%IDLE_MINUTES%"=="" set IDLE_PARAM=--idle-minutes %IDLE_MINUTES%

set NODE_PARAM=
if not "%NODE_ID%"=="" set NODE_PARAM=--node-id %NODE_ID% --name %NODE_ID%

echo.
if "%SERVER_URL%"=="" goto push_mode

echo ========================================================
echo Starting worker in PULL (Outbound) Mode
echo Connecting to Server: %SERVER_URL%
echo Configuration File:  %~dp0server_url.txt
if "%NODE_ID%"=="" echo * Worker Node ID:   Auto (Hostname)
if not "%NODE_ID%"=="" echo * Worker Node ID:   %NODE_ID%
if "%IDLE_MINUTES%"=="" echo * Idle Requirement: Dynamic (Synchronized with Server)
if not "%IDLE_MINUTES%"=="" echo * Idle Requirement: %IDLE_MINUTES% min (0 = Dedicated Compute Mode)
echo * Outbound only: Bypasses hospital inbound firewalls
echo ========================================================
echo.
python vpsqa_worker.py --server-url "%SERVER_URL%" --mcsquare-dir "%MCSQUARE_DIR%" %IDLE_PARAM% %NODE_PARAM%
goto end

:push_mode
echo ========================================================
echo Starting worker in PUSH Mode on port 8001
echo ========================================================
echo.
python vpsqa_worker.py --port 8001 --mcsquare-dir "%MCSQUARE_DIR%" %IDLE_PARAM% %NODE_PARAM%

:end
pause
