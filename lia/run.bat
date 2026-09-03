@echo off
:: Launch Lia (needs admin for the global keyboard hook).
::
:: If the "Lia" scheduled task exists (created from the app's
:: Options -> "Start with Windows"), launch THROUGH it: schtasks /run starts the
:: task elevated with NO UAC prompt. Only when the task isn't there yet do we
:: bootstrap by self-elevating once (the one unavoidable UAC consent).

cd /d "%~dp0"

:: 1) Preferred path - no UAC: run the registered elevated task.
schtasks /query /tn "Lia" >nul 2>&1
if not errorlevel 1 (
    schtasks /run /tn "Lia" >nul 2>&1
    if not errorlevel 1 exit /b
)

:: 2) Bootstrap (no task yet): elevate once via UAC, then start.
::    Absolute powershell path (System32) so a stray powershell.exe in the
::    launch dir can't be run instead; the bat path is passed via an env var,
::    not interpolated into the PowerShell string, so a path with a quote or a
::    PowerShell metacharacter can't break out of it (2026-09-03 audit #7/#13).
net session >nul 2>&1
if errorlevel 1 (
    set "LIA_SELF=%~f0"
    "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath $env:LIA_SELF -Verb RunAs"
    exit /b
)

:: 3) Launch. The distribution ships an absolute-pathed Lia.exe next to this
::    file; the bare-pythonw branch is only for a raw source checkout on a dev
::    machine (same-user dir), where the shipped launchers are not present.
if exist "%~dp0Lia.exe" (
    start "" "%~dp0Lia.exe" "%~dp0lia.py"
) else (
    start "" pythonw "%~dp0lia.py"
)
