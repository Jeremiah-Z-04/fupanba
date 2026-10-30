@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 每日一键：跑复盘 -> 推框架（变了才推）-> 导出静态数据 -> 推送到 GitHub Pages
rem 双击即可。非交易日跑也没关系，没变化就不会产生提交。

set "PY="
if exist "C:\Users\13044\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=C:\Users\13044\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY if exist "C:\Program Files\Python311\python.exe" set "PY=C:\Program Files\Python311\python.exe"
if not defined PY set "PY=python"

echo [1/4] 采集 + 复盘...
"%PY%" main.py run --no-clip
if errorlevel 1 (
  echo 复盘失败，已中止。历史数据不会被改动。
  pause
  exit /b 1
)

echo.
echo [2/4] 发布题材框架（内容没变会自动跳过）...
"%PY%" deploy\publish_framework.py

echo.
echo [3/4] 导出静态数据...
"%PY%" deploy\export_static.py
if errorlevel 1 (
  echo 导出失败，看上面的提示。
  pause
  exit /b 1
)

echo.
echo [4/4] 推送到 GitHub Pages...
"%PY%" deploy\publish_static.py
if errorlevel 1 (
  echo 发布失败，看上面的提示。
  pause
  exit /b 1
)

echo.
echo 完成。手机打开 GitHub Pages 地址即可（CDN 可能要等 1 分钟）。
pause
