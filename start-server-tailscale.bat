@echo off
setlocal
cd /d "%~dp0"

echo Starting ShazChat for Tailscale testing...
start "ShazChat Server" /min cmd /c "python server.py"
timeout /t 2 /nobreak >nul

tailscale.exe serve --https=8445 --bg --yes http://127.0.0.1:8765
echo.
echo Tailscale test address: wss://taylor.tail5d09b2.ts.net:8445
echo Keep this computer on and connected to Tailscale while testing.
pause
