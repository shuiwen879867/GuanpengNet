@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (python deploy.py all %*) else (py -3 deploy.py all %*)
if errorlevel 1 pause
