@echo off
setlocal
cd /d "%~dp0"

:menu
cls
echo ShazChat Server Administration
echo ==============================
echo 1. List players
echo 2. List bans
echo 3. List mutes
echo 4. Ban a player
echo 5. Mute a player
echo 6. Unban a player
echo 7. Unmute a player
echo 8. View last 50 chat messages
echo 9. Reset a player's password
echo 0. Exit
set /p choice=Choose an action:

if "%choice%"=="1" python admin.py players
if "%choice%"=="2" python admin.py bans
if "%choice%"=="3" python admin.py mutes
if "%choice%"=="4" goto ban
if "%choice%"=="5" goto mute
if "%choice%"=="6" goto unban
if "%choice%"=="7" goto unmute
if "%choice%"=="8" python admin.py logs
if "%choice%"=="9" goto reset
if "%choice%"=="0" exit /b
pause
goto menu

:ban
set /p player=Player name:
set /p reason=Reason (optional):
python admin.py ban "%player%" --reason "%reason%"
pause
goto menu

:mute
set /p player=Player name:
set /p minutes=Minutes (0 = permanent):
set /p reason=Reason (optional):
python admin.py mute "%player%" --minutes %minutes% --reason "%reason%"
pause
goto menu

:unban
set /p player=Player name:
python admin.py unban "%player%"
pause
goto menu

:unmute
set /p player=Player name:
python admin.py unmute "%player%"
pause
goto menu

:reset
set /p player=Player name:
python admin.py reset-password "%player%"
pause
goto menu
