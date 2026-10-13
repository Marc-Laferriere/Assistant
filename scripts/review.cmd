@echo off
title Review Tasks
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m assistant review
echo.
pause
