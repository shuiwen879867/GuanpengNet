@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (python deploy.py bot %*) else (py -3 deploy.py bot %*)
if errorlevel 1 pause
