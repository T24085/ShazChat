@echo off
cd /d "%~dp0"
if not exist "release\ShazChat.exe" (
  echo ShazChat.exe has not been built yet.
  echo Run build-exe-simple.bat first.
  pause
  exit /b 1
)

start "" "release\ShazChat.exe"
