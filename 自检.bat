@echo off
title Self Test - Capture and Classification Tool
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

echo ============================================
echo   Self test: scheduler / plans / params
echo ============================================
echo.

set "ELECTRON=%~dp0node_modules\electron\dist\electron.exe"
if not exist "%ELECTRON%" (
    echo [ERROR] Electron not found: %ELECTRON%
    echo Run "npm install" in this folder first.
    echo.
    pause
    exit /b 1
)

rem Reuse the bundled Electron binary as a plain Node runtime.
set ELECTRON_RUN_AS_NODE=1
"%ELECTRON%" "%~dp0selftest.js"
set RESULT=%errorlevel%
set ELECTRON_RUN_AS_NODE=

echo.
if %RESULT% equ 0 (
    echo [OK] All checks passed.
) else (
    echo [FAILED] Some checks failed - see details above.
)
echo.
echo This test only checks pure logic (scheduling, plan parsing, parameter
echo building). It does NOT touch the network, the account manager, or
echo your real plans/history files.
echo.
pause
exit /b %RESULT%
