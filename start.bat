@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ---- locate Python (project venv -> system -> PATH) ----
set "PY="
if exist "C:\Users\13044\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=C:\Users\13044\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY if exist "C:\Program Files\Python311\python.exe" set "PY=C:\Program Files\Python311\python.exe"
if not defined PY set "PY=python"

echo [ThemeHunter] starting...
"%PY%" start.py
if errorlevel 1 pause
