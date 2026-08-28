@echo off
:: Launch Lia WITHOUT a UAC prompt.
:: Requires install_no_uac.bat to have been run once as Administrator.
::
:: Mechanism: invokes the "Lia" Scheduled Task created by
:: install_no_uac.bat. Tasks with /rl HIGHEST that were authorised at
:: creation time bypass UAC on every subsequent run.
::
:: If the task isn't installed yet, falls back to run.bat (which still
:: prompts UAC) and tells the user to install for silent launches.

setlocal

set "TASK_NAME=Lia"

:: Is the scheduled task registered?
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  No-UAC launch not available — Scheduled Task "%TASK_NAME%" not found.
    echo  To install ^(one-time UAC prompt^), run install_no_uac.bat as Administrator.
    echo.
    echo  Falling back to run.bat ^(will prompt UAC^).
    echo.
    pause
    call "%~dp0run.bat"
    exit /b
)

:: Run the task. This is the no-UAC path.
schtasks /run /tn "%TASK_NAME%"
if errorlevel 1 (
    echo.
    echo  Failed to start the Scheduled Task. Falling back to run.bat.
    echo.
    pause
    call "%~dp0run.bat"
    exit /b
)

:: Success — task started silently, no UAC prompt. Window can close.
exit /b 0
