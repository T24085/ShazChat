@echo off
setlocal
cd /d "%~dp0"

set "CAPTIMER_SERVER=wss://taylor.tail5d09b2.ts.net:8445"
echo Starting ShazChat through Tailscale...
python main.py --server "%CAPTIMER_SERVER%"
