@echo off
title Virtual PSQA - Idle MCsquare Worker
echo ========================================================
echo   Virtual PSQA - Distributed Monte Carlo Worker
echo ========================================================
echo.
set PORT=8001
set MCSQUARE_DIR=..\MCsquare

if not exist "%MCSQUARE_DIR%" (
    set MCSQUARE_DIR=.\MCsquare
)

echo Starting worker node on port %PORT% using %MCSQUARE_DIR%...
echo This workstation will automatically calculate beams when idle.
echo If a user is active, calculations will automatically yield.
echo.

python vpsqa_worker.py --port %PORT% --mcsquare-dir "%MCSQUARE_DIR%" --idle-minutes 5.0 --max-cpu-pct 30.0

pause
