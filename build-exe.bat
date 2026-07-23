@echo off
echo Building ShazChat executable...
echo.

REM Clean previous builds
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"
if exist "*.spec" del /q "*.spec"

REM Build with PyInstaller
pyinstaller --onefile ^
    --windowed ^
    --name "ShazChat" ^
    --icon=appertimer.ico ^
    --hidden-import PyQt6.QtCore ^
    --hidden-import PyQt6.QtGui ^
    --hidden-import PyQt6.QtWidgets ^
    --hidden-import keyboard ^
    --hidden-import win32gui ^
    --hidden-import win32con ^
    --hidden-import websockets ^
    --hidden-import asyncio ^
    --collect-all websockets ^
    --collect-all PyQt6 ^
    main.py

echo.
echo Build complete! Check the 'dist' folder for ShazChat.exe
pause
