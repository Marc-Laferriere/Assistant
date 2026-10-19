@echo off
rem Registers a folder of linked PDFs for a project (and lists the ones already known).
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m assistant pdf-folder --list
echo.
".venv\Scripts\python.exe" -m assistant pdf-folder
echo.
pause
