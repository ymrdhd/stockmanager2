@echo off
rem Stock Manager: double-click launcher for the local preview web page.
rem It only serves a synthetic sandbox under data\preview-app; the formal
rem database is never opened here.
setlocal
cd /d "%~dp0"
title Stock Manager - local preview
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" goto :novenv
".venv\Scripts\python.exe" "tools\open_preview.py"
if errorlevel 1 goto :failed
goto :end

:novenv
echo.
echo [ERROR] Python virtual environment .venv not found.
echo         Run this once in this folder:  uv sync --locked
echo.
goto :end

:failed
echo.
echo [ERROR] Preview could not start. See the messages above.
echo.

:end
echo.
echo Press any key to close this window.
pause >nul
