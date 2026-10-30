@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 手机访问用：监听局域网，启动后会打印手机能直接打开的地址。
rem 同一 WiFi 下的任何设备都能访问，建议只在家庭网络下使用。

set "PY="
if exist "C:\Users\13044\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=C:\Users\13044\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY if exist "C:\Program Files\Python311\python.exe" set "PY=C:\Program Files\Python311\python.exe"
if not defined PY set "PY=python"

echo [ThemeHunter] starting in LAN mode...
"%PY%" start.py --lan
if errorlevel 1 pause
