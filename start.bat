@echo off
set PYTHONUNBUFFERED=1
REM ============================================================
REM  Virtual PSQA - Start Application (Backend + Web UI)
REM ============================================================
setlocal enabledelayedexpansion
set "ROOT=%~dp0"
set "PYTHONIOENCODING=utf-8"

if not defined PSQA_PORT set "PSQA_PORT=8000"

REM 1. Detect Python executable
set "PY="
if exist "%ROOT%python\python.exe" (
    set "PY=%ROOT%python\python.exe"
) else if exist "%ROOT%.venv\Scripts\python.exe" (
    set "PY=%ROOT%.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%I in ('where python') do (
            if not defined PY set "PY=%%I"
        )
    )
)

if not defined PY (
    echo.
    echo ============================================================
    echo  ERROR: Python runtime not found.
    echo ============================================================
    echo  Could not locate python\python.exe or .venv\Scripts\python.exe.
    echo  Please install Python 3.11+ and run setup.bat, or ensure
    echo  the bundled python folder is present.
    echo ============================================================
    echo.
    pause
    exit /b 1
)

REM 2. Verify frontend distribution build exists
if not exist "%ROOT%frontend\dist\index.html" (
    echo.
    echo ============================================================
    echo  ERROR: Web frontend build not found at frontend\dist\
    echo ============================================================
    echo  index.html is missing from frontend\dist.
    echo  Please build the frontend or copy the pre-built dist folder.
    echo ============================================================
    echo.
    pause
    exit /b 1
)

REM 3. Ensure backend\.env exists
if not exist "%ROOT%backend\.env" (
    if exist "%ROOT%backend\.env.example" (
        copy "%ROOT%backend\.env.example" "%ROOT%backend\.env" >nul
        echo [INFO] Created backend\.env from template (.env.example).
    )
)

REM 4. Ensure runtime data folders exist
if not exist "%ROOT%backend\data\dicom_store" mkdir "%ROOT%backend\data\dicom_store" >nul 2>nul
if not exist "%ROOT%backend\data\results" mkdir "%ROOT%backend\data\results" >nul 2>nul

REM 5. Launch FastAPI backend & server
cd /d "%ROOT%backend"
echo ============================================================
echo  Virtual PSQA Server
echo ============================================================
echo  Local UI:    http://localhost:%PSQA_PORT%
echo  Network UI:  http://%COMPUTERNAME%:%PSQA_PORT%
echo  Python:      %PY%
echo.
echo  Press Ctrl+C to stop the server.
echo ============================================================
echo.

"%PY%" -m uvicorn main:app --host 0.0.0.0 --port %PSQA_PORT%

if errorlevel 1 (
    echo.
    echo [ERROR] Server exited with code %errorlevel%.
    pause
)