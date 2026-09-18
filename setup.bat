@echo off
REM ============================================================
REM  Virtual PSQA - one-time bundle setup (run on a build machine)
REM  Creates .venv with all Python packages and builds frontend\dist.
REM  Copy the entire project folder to the clinic server afterward.
REM  Requires: Python 3.11+ and Node.js 18+ on PATH (build only).
REM ============================================================
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

echo === Virtual PSQA bundle setup ===
echo.

set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [1/4] Creating bundled Python environment ^(.venv^)...
  python -m venv "%ROOT%.venv"
  if errorlevel 1 (
    echo.
    echo ERROR: could not create .venv — is Python 3.11+ on PATH?
    pause & exit /b 1
  )
) else (
  echo [1/4] Bundled Python environment already exists — reusing .venv
)

echo [2/4] Installing Python packages into .venv...
"%ROOT%.venv\Scripts\python.exe" -m pip install --upgrade pip
"%ROOT%.venv\Scripts\python.exe" -m pip install -r "%ROOT%requirements.txt"
if errorlevel 1 (
  echo.
  echo ERROR: failed to install Python dependencies.
  pause & exit /b 1
)

echo [3/4] Building web frontend into frontend\dist...
if not exist "%ROOT%frontend\package.json" (
  echo ERROR: frontend\package.json not found.
  pause & exit /b 1
)
cd /d "%ROOT%frontend"
if not exist "node_modules" (
  call npm install
  if errorlevel 1 (
    echo ERROR: npm install failed. Is Node.js installed and on PATH?
    pause & exit /b 1
  )
) else (
  echo       node_modules present — skipping npm install
)
call npm run build
if errorlevel 1 (
  echo ERROR: frontend build failed.
  pause & exit /b 1
)

cd /d "%ROOT%"
echo [4/4] Preparing configuration...
if not exist "%ROOT%backend\.env" (
  copy "%ROOT%backend\.env.example" "%ROOT%backend\.env" >nul
  echo       Created backend\.env from the example template.
) else (
  echo       backend\.env already exists — left unchanged.
)

echo. > "%ROOT%.bundle_ready"

echo.
echo ============================================================
echo  Bundle ready.
echo.
echo  To deploy: copy this ENTIRE folder to the clinic server
echo             ^(include .venv and frontend\dist^).
echo  On the server: double-click run.bat or start.bat — no Python
echo                 or Node.js install required.
echo.
echo  Next: edit backend\.env ^(MCsquare paths, watch folder^).
echo ============================================================
pause
