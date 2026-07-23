@echo off
setlocal

set "SHAZCHAT_EXE=%LOCALAPPDATA%\Programs\ShazChat\ShazChat.exe"
set "CAPTIMER_SERVER=wss://taylor.tail5d09b2.ts.net:8445"

if not exist "%SHAZCHAT_EXE%" (
  echo ShazChat was not found at:
  echo %SHAZCHAT_EXE%
  echo Install ShazChat first, then run this file again.
  pause
  exit /b 1
)

echo Starting ShazChat through the temporary Tailscale test server...
start "" "%SHAZCHAT_EXE%" --server "%CAPTIMER_SERVER%"
