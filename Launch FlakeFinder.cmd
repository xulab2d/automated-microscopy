@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m flakefinder gui
if errorlevel 1 pause
