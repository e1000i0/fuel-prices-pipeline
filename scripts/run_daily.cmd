@echo off
rem Daily run for Windows Task Scheduler. Appends to data\logs\daily.log (lines are timestamped).
rem Register once:  schtasks /Create /TN fuel-prices-daily /SC DAILY /ST 09:00 /TR "\"%~f0\""
cd /d "%~dp0.."
if not exist data\logs mkdir data\logs
".venv\Scripts\python.exe" -m pipeline run >> "data\logs\daily.log" 2>&1
exit /b %ERRORLEVEL%
