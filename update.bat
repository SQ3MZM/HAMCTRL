@echo off
REM update.bat - downloads the latest HAMCTRL installer from the private
REM GitHub release and offers to run it. Place this file anywhere on the
REM test machine (Desktop is fine) and double-click it.

setlocal
set "REPO=SQ3MZM/HAMCTRL"
set "GH=C:\Program Files\GitHub CLI\gh.exe"
if not exist "%GH%" set "GH=gh"
set "DEST=%USERPROFILE%\Desktop"

echo Downloading latest HAMCTRL build...
"%GH%" release download latest --repo %REPO% --pattern "*.exe" --dir "%DEST%" --clobber
if errorlevel 1 (
    echo.
    echo Download failed. Check login: "%GH%" auth status
    pause
    exit /b 1
)

echo.
echo Downloaded to: %DEST%\HAMCTRL-Setup.exe
set /p RUN="Run the installer now? (y/n): "
if /i "%RUN%"=="y" start "" "%DEST%\HAMCTRL-Setup.exe"
pause
