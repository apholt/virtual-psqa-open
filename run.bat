@echo off
REM ============================================================
REM  Virtual PSQA - Portable Launcher
REM  Double-click to start server and web interface.
REM ============================================================
setlocal
set "ROOT=%~dp0"
call "%ROOT%start.bat" %*
