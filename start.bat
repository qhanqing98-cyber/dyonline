@echo off
chcp 65001 >nul
setlocal
title DY-ONLINE 视频下载台

cd /d "%~dp0"

echo ============================================
echo   DY-ONLINE 视频下载台 - 一键启动
echo ============================================
echo.

rem ---------- 1. 虚拟环境 ----------
if not exist ".venv\Scripts\python.exe" (
    echo [1/4] 未检测到虚拟环境，正在创建 .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败，请确认已安装 Python 3.10 及以上版本。
        pause
        exit /b 1
    )
    echo [1/4] 虚拟环境创建完成
) else (
    echo [1/4] 虚拟环境已就绪
)

rem ---------- 2. 安装依赖 ----------
echo [2/4] 正在检查并安装依赖 ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
)

rem ---------- 3. 环境就绪 ----------
echo [3/4] 环境就绪

rem ---------- 4. 启动服务 + 打开浏览器 ----------
echo [4/4] 正在启动 Web 服务，3 秒后自动打开浏览器...
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8000'"

echo.
echo 服务地址: http://127.0.0.1:8000
echo 按 Ctrl+C 可停止服务
echo ============================================
echo.

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

echo.
echo 服务已停止。
pause
