@echo off
:: Remove the Lia Scheduled Task created by install_no_uac.bat.
:: Run as Administrator (one-time UAC prompt to remove the task).

setlocal

net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo  This script must be run as Administrator.
    echo  Right-click "uninstall_no_uac.bat" and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

set "TASK_NAME=Lia"

schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  No scheduled task named "%TASK_NAME%" was found. Nothing to remove.
    echo.
    pause
    exit /b 0
)

schtasks /delete /tn "%TASK_NAME%" /f
if errorlevel 1 (
    echo.
    echo  Failed to delete scheduled task. See errors above.
    pause
    exit /b 1
)

echo.
echo  Scheduled task "%TASK_NAME%" removed.
echo  Lia will no longer auto-start at login.
echo  To launch manually, double-click run.bat ^(prompts UAC again^)
echo  or re-run install_no_uac.bat to re-enable no-UAC startup.
echo.
pause
