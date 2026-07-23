@echo off
setlocal
cd /d "%~dp0"
python experiments\capper_vision_lab\vision_lab.py
if errorlevel 1 pause
