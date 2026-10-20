@echo off
title Create Desktop Shortcut
cd /d "%~dp0"

echo Creating desktop shortcut...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\make_shortcut.ps1"

if %errorlevel% equ 0 (
    echo.
    echo [OK] Desktop shortcut created.
    echo Double-click the desktop icon to launch. No console window.
) else (
    echo.
    echo [FAILED] Could not create shortcut.
)

echo.
pause
