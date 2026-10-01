@echo off
title Virtual PSQA - MCsquare Worker (Push Mode port 8001)
echo ========================================================
echo   Virtual PSQA - Distributed Monte Carlo Worker
echo   Operational Mode: PUSH (Listening on port 8001)
echo ========================================================
echo.

cd /d "%~dp0"
set PORT=8001
set MCSQUARE_DIR=..\MCsquare
if not exist "%MCSQUARE_DIR%" set MCSQUARE_DIR=.\MCsquare

echo Checking dependencies...
python -c "import fastapi, uvicorn, numpy" 2>nul
if %errorlevel% neq 0 echo Installing worker dependencies...
if %errorlevel% neq 0 python -m pip install -r requirements.txt

echo.
echo Starting worker node in PUSH Mode on port %PORT%...
echo MCsquare directory: %MCSQUARE_DIR%
echo.
python vpsqa_worker.py --port %PORT% --mcsquare-dir "%MCSQUARE_DIR%" --idle-minutes 0
pause
