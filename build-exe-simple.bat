@echo off
echo ========================================
echo ShazChat - Executable Builder
echo ========================================
echo.

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found!
    echo Please make sure Python is installed and in your PATH.
    pause
    exit /b 1
)

REM Check if PyInstaller is installed
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    echo.
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install PyInstaller
        echo Please install manually: python -m pip install pyinstaller
        pause
        exit /b 1
    )
    echo.
)

echo.
echo Cleaning the release staging area...
if exist "build\ShazChat" rmdir /s /q "build\ShazChat"
if exist "release\ShazChat.exe" del /q "release\ShazChat.exe"
if not exist "release" mkdir "release"

echo.
echo Building executable (this may take a few minutes)...
echo.

REM Build with PyInstaller - one file, no console window
python -m PyInstaller --onefile ^
    --windowed ^
    --name "ShazChat" ^
    --icon "%CD%\appertimer.ico" ^
    --hidden-import PyQt6.QtCore ^
    --hidden-import PyQt6.QtGui ^
    --hidden-import PyQt6.QtWidgets ^
    --hidden-import win32gui ^
    --hidden-import win32con ^
    --hidden-import websockets ^
    --hidden-import asyncio ^
    --collect-all cryptography ^
    --collect-all websockets ^
    --collect-all PyQt6 ^
    --distpath release ^
    --workpath build\ShazChat ^
    --specpath build\ShazChat ^
    main.py

if errorlevel 1 (
    echo.
    echo ERROR: Build failed!
    pause
    exit /b 1
)

echo.
echo ========================================
echo Build successful!
echo ========================================
echo.
echo The executable is ready: release\ShazChat.exe
echo.
echo To distribute:
echo 1. Copy ShazChat.exe from the release folder
echo 2. Copy ShazChat.bat (the launcher)
echo 3. Copy README-DISTRIBUTION.txt (instructions)
echo 4. Zip these 3 files and share with your team
echo.
pause
