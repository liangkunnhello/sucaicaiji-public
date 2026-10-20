@echo off
title Rule Check - 分类规则自检
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set "PY=%~dp0classify-engine\runtime\python\python.exe"
if not exist "%PY%" (
    echo [错误] 找不到内置 Python: %PY%
    echo        请先确认 classify-engine\runtime 完整。
    echo.
    pause
    exit /b 1
)

echo ============================================
echo   一、规则一致性校验
echo ============================================
"%PY%" "%~dp0classify-engine\tools\check_rules.py"
set RESULT=%errorlevel%

echo.
if %RESULT% equ 0 (
    echo [通过] 规则自洽，可以跑批次。
) else (
    echo [有问题] 上面列出的 ERROR 会直接造成错分或丢分，建议先修。
)

rem 拖一个审计 jsonl 到本文件上，会额外出一份该批次的健康报告
if not "%~1"=="" (
    echo.
    echo ============================================
    echo   二、健康报告： %~nx1
    echo ============================================
    "%PY%" "%~dp0classify-engine\tools\rule_health.py" --audit "%~1"
)

echo.
echo --------------------------------------------
echo 其他两个工具（需要审计文件，位于 分类结果\_API分类审计.jsonl）：
echo.
echo   [规则健康报告] 自动做门控归因、零命中词、新方向候选
echo     python classify-engine\tools\rule_health.py --audit "审计.jsonl" --source "原始素材"
echo.
echo   [改规则前后对比] 离线重放，不调 API、不动缓存
echo     python classify-engine\tools\replay_audit.py --audit "审计.jsonl" --dump before.json
echo     python classify-engine\tools\replay_audit.py --audit "审计.jsonl" --dump after.json --compare before.json
echo.
echo   （python 用 classify-engine\runtime\python\python.exe）
echo.
echo 也可以把审计 jsonl 直接拖到本文件上，直接看健康报告。
echo --------------------------------------------
echo.
pause
exit /b %RESULT%
