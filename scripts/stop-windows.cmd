@echo off
rem Stop the three PPT AS services started by scripts\services-windows.ps1.
rem Usage: double-click this file, or run it from a terminal.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0services-windows.ps1" -Action stop %*
set EXITCODE=%ERRORLEVEL%
echo.
echo Exit code: %EXITCODE%
pause
exit /b %EXITCODE%
