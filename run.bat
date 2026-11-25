@echo off
title TargetPulse — Academic Outreach Studio
color 0B
cls

echo =================================================================
echo   TargetPulse - Academic Outreach & Contact Discovery Studio
echo =================================================================
echo.

:: Check if Python is installed
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python was not found on this computer.
    echo.
    echo Please install Python 3 (version 3.8 or newer) from:
    echo   https://www.python.org/downloads/
    echo or install "Python" from the Windows Microsoft Store.
    echo.
    pause
    exit /b 1
)

:: Free port 8765 if an old instance is lingering
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr :8765 ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1
)

echo Starting TargetPulse Studio...
echo Local Access: http://127.0.0.1:8765
echo.
echo NOTE: Keep this window open while using the app.
echo       Press Ctrl+C to stop at any time.
echo =================================================================
echo.

python app.py --host 0.0.0.0 --port 8765
pause
