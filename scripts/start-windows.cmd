@echo off
rem Start the three PPT AS services (agent 8001 / backend 3002 / frontend 5174).
rem Usage: double-click this file, or run it from a terminal.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0services-windows.ps1" -Action start %*
set EXITCODE=%ERRORLEVEL%
echo.
echo Exit code: %EXITCODE%
pause
exit /b %EXITCODE%
