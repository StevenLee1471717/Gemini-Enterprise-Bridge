@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [Error] Bridge launcher exited with code: %ERRORLEVEL%
    pause
    exit /b %ERRORLEVEL%
)
