@echo off
cd /d "%~dp0"
py -3 Launcher.py
if errorlevel 1 pause
