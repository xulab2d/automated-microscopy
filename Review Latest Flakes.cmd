@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m flakefinder review "data/live-validation-20261008-c/scan/analysis"
if errorlevel 1 pause
