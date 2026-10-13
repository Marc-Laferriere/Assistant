@echo off
REM Runs shortly before the workday starts: full cycle, then opens the brief.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m assistant run --open-brief
exit /b %ERRORLEVEL%
