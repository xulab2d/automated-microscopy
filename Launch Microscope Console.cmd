@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\launch_scan_console.py
if errorlevel 1 pause
