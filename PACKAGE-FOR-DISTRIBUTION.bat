@echo off
echo ========================================
echo ShazChat - Package Creator
echo ========================================
echo.

REM Check if dist folder exists
if not exist "release\ShazChat.exe" (
    echo ERROR: Updated ShazChat.exe not found!
    echo Please run build-exe-simple.bat first to build the executable.
    pause
    exit /b 1
)

echo Creating distribution package...
echo.

REM Create distribution folder
if exist "ShazChat-Distribution" rmdir /s /q "ShazChat-Distribution"
mkdir "ShazChat-Distribution"

REM Copy files
copy "release\ShazChat.exe" "ShazChat-Distribution\ShazChat.exe" >nul
copy "ShazChat.bat" "ShazChat-Distribution\" >nul
copy "README-DISTRIBUTION.txt" "ShazChat-Distribution\" >nul

echo Files copied to: ShazChat-Distribution\
echo.
echo Distribution package ready!
echo.
echo Contents:
echo   - ShazChat.exe (the application)
echo   - ShazChat.bat (launcher - double click this)
echo   - README-DISTRIBUTION.txt (instructions)
echo.
echo Next steps:
echo 1. Zip the ShazChat-Distribution folder
echo 2. Share the zip file with your teammates
echo 3. They extract and double-click ShazChat.bat
echo.
pause
