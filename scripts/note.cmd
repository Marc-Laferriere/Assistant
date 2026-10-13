@echo off
title Add Note
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m assistant note
echo.
pause
