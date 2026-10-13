@echo off
title Add Task
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m assistant capture --add
if errorlevel 1 (
    echo.
    echo [error] see logs\assistant.log
    pause
) else (
    timeout /t 2 >nul
)
