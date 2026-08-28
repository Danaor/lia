@echo off
:: Launch Lia (needs admin for the global keyboard hook).
::
:: If the "Lia" scheduled task exists (created from the app's
:: Options -> "Start with Windows"), launch THROUGH it: schtasks /run starts the
:: task elevated with NO UAC prompt. Only when the task isn't there yet do we
:: bootstrap by self-elevating once (the one unavoidable UAC consent).

cd /d "%~dp0"

:: 1) Preferred path — no UAC: run the registered elevated task.
schtasks /query /tn "Lia" >nul 2>&1
if not errorlevel 1 (
    schtasks /run /tn "Lia" >nul 2>&1
    if not errorlevel 1 exit /b
)

:: 2) Bootstrap (no task yet): elevate once via UAC, then start.
net session >nul 2>&1
if errorlevel 1 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

if exist "Lia.exe" (
    start "" "%~dp0Lia.exe" "%~dp0lia.py"
) else (
    start "" pythonw lia.py
)
