@echo off
rem Prepare the ppt_as runtime dependencies on Windows.
rem All caches and install artifacts stay on the project drive (G:), never on C:.
rem Usage: double-click this file, or run it from a terminal.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-windows.ps1" %*
set EXITCODE=%ERRORLEVEL%
echo.
echo Exit code: %EXITCODE%
pause
exit /b %EXITCODE%
