@echo off
rem FlipComp daily run: hunt leads, scan deals, diff against yesterday, write the report.
rem Register it once with Task Scheduler (run from this folder):
rem   schtasks /create /tn "FlipComp daily" /tr "\"%~dp0daily.bat\"" /sc daily /st 06:30 /f
cd /d "%~dp0"
python hunt.py --daily --open >> "%~dp0reports\daily.log" 2>&1
