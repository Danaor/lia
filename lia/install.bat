@echo off
chcp 65001 >nul 2>&1
echo.
echo  ╔══════════════════════════════════════════╗
echo  ║     Lia - Full Installation      ║
echo  ║     Local Speech-to-Text for Windows     ║
echo  ╚══════════════════════════════════════════╝
echo.

:: Check Python
echo [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: Python is not installed or not in PATH.
    echo  Please install Python 3.11+ from https://python.org
    echo  Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)
python --version
echo  OK!
echo.

:: Install dependencies
echo [2/4] Installing Python dependencies...
echo.
python -m pip install --upgrade pip >nul 2>&1
python -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo  WARNING: Some packages may have failed.
    echo  Trying PyAudio fix...
    python -m pip install pipwin
    pipwin install pyaudio
)
echo.
echo  Dependencies installed!
echo.

:: Download models
echo [3/4] Downloading AI models...
echo.
echo  Choose which models to download:
echo    1) Recommended - Hebrew Turbo only (~1.5 GB)
echo    2) Hebrew models only (~4.5 GB)
echo    3) All models (~11 GB)
echo.
set /p MODEL_CHOICE="  Enter choice (1/2/3) [default: 1]: "
if "%MODEL_CHOICE%"=="" set MODEL_CHOICE=1

if "%MODEL_CHOICE%"=="1" (
    python "%~dp0download_models.py" --recommended
) else if "%MODEL_CHOICE%"=="2" (
    python "%~dp0download_models.py" --hebrew-only
) else (
    python "%~dp0download_models.py"
)
echo.

:: Add to startup?
echo [4/4] Setup complete!
echo.
set /p STARTUP_CHOICE="  Add Lia to Windows startup? (y/n) [default: n]: "
if /i "%STARTUP_CHOICE%"=="y" (
    call "%~dp0add_to_startup.bat"
)

echo.
echo  ╔══════════════════════════════════════════╗
echo  ║       Installation complete!             ║
echo  ║                                          ║
echo  ║  To run:  Double-click "run.bat"         ║
echo  ║  Hotkey:  Hold Ctrl+Space to record      ║
echo  ║  Menu:    Right-click tray icon           ║
echo  ╚══════════════════════════════════════════╝
echo.
pause
