@echo off
title Material Capture and Classification Tool
cd /d "%~dp0"

echo ============================================
echo   Material Capture + Classification Tool
echo   (console mode - shows startup errors)
echo ============================================
echo.
echo NOTE: Keep this window OPEN while the app runs.
echo       The built-in scheduler only works while the app is running.
echo.

set "ELECTRON=%~dp0node_modules\electron\dist\electron.exe"

if not exist "%ELECTRON%" (
    echo [ERROR] Electron not found: %ELECTRON%
    echo Please run  npm install  in this folder first.
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0main.js" (
    echo [ERROR] main.js not found. Wrong folder?
    echo.
    pause
    exit /b 1
)

echo Launching UI, please wait...
echo Closing this window will also close the app.
echo.

"%ELECTRON%" "%~dp0." %*

echo.
echo App exited.
pause
