@echo off
setlocal
cd /d "%~dp0"

:: Avoid a confusing crash when the server is already running.
powershell.exe -NoProfile -Command "$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if ($listener) { exit 0 } else { exit 1 }"
if not errorlevel 1 (
    echo.
    echo ShazChat server is already running on port 8765.
    echo The Cloudflare tunnel is ready for players to connect.
    echo.
    pause
    exit /b 0
)

echo.
echo Starting ShazChat server for wss://capper.novatec.casa...
echo Keep this window open while people are playing.
echo.
python server.py

echo.
echo The ShazChat server stopped. Press any key to close this window.
pause
