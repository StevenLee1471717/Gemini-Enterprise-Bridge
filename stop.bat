@echo off
chcp 65001 >nul
set PYTHONUTF8=1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Stop %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [Warn] Stop command exited with code: %ERRORLEVEL%
    pause
    exit /b %ERRORLEVEL%
)
