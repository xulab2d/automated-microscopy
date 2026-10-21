@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\launch_flake_review.py
if errorlevel 1 pause
