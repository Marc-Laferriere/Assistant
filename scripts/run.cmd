@echo off
REM Full hourly cycle: process capture.txt, then run the Submittal mirror.
REM Logs go to logs\assistant.log. Safe to miss runs.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m assistant run
exit /b %ERRORLEVEL%
