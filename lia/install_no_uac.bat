@echo off
:: Install Lia as a Scheduled Task with highest privileges.
:: Once installed, Lia launches automatically at login WITHOUT
:: a UAC prompt. The single UAC prompt happens only when running THIS
:: script (one-time setup).
::
:: Why: Lia needs admin rights for the Windows global keyboard
:: hook (the `keyboard` library). The default run.bat uses
:: `Start-Process -Verb RunAs`, which prompts UAC every launch.
:: Scheduled Tasks with HIGHEST run-level are authorised once at
:: creation time and bypass UAC on subsequent invocations.

setlocal

net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo  This script must be run as Administrator.
    echo  Right-click "install_no_uac.bat" and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

set "TASK_NAME=Lia"
set "SCRIPT_DIR=%~dp0"
set "EXE_PATH=%SCRIPT_DIR%Lia.exe"
set "SCRIPT_PATH=%SCRIPT_DIR%lia.py"

if not exist "%EXE_PATH%" (
    echo.
    echo  Lia.exe not found in this folder.
    echo  Launch Lia once via run.bat first - it generates
    echo  Lia.exe automatically on first run, then run this
    echo  installer.
    echo.
    pause
    exit /b 1
)

:: Remove any existing startup shortcut so we don't end up with TWO
:: copies trying to launch (Scheduled Task + Startup folder shortcut).
set "STARTUP_LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Lia.lnk"
if exist "%STARTUP_LNK%" (
    del "%STARTUP_LNK%"
    echo  Removed existing Startup shortcut to avoid duplicate launches.
)

:: Remove any pre-existing task with the same name (idempotent install).
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if not errorlevel 1 (
    schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1
)

:: Build the action command. Quoting via XML is the most reliable
:: way to handle paths with spaces, but plain schtasks works for
:: most installs. Use /tr with the exe + script as arguments.
schtasks /create ^
    /tn "%TASK_NAME%" ^
    /tr "\"%EXE_PATH%\" \"%SCRIPT_PATH%\"" ^
    /sc ONLOGON ^
    /rl HIGHEST ^
    /delay 0000:05 ^
    /f

if errorlevel 1 (
    echo.
    echo  Failed to create scheduled task. See errors above.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo   Lia installed as Scheduled Task.
echo  ============================================================
echo.
echo   - Auto-starts at login with admin rights ^(no UAC prompt^).
echo   - Launches 5 seconds after login to let the desktop settle.
echo.
echo   To launch on demand without waiting for re-login:
echo     schtasks /run /tn "%TASK_NAME%"
echo.
echo   To uninstall: run uninstall_no_uac.bat ^(also as admin^).
echo.
pause
