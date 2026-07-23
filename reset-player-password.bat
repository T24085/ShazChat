@echo off
cd /d "%~dp0"
echo ShazChat - Server Account Password Reset
echo.
python tools\reset_player_password.py
echo.
pause
