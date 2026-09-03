@echo off
:: Add Lia to Windows startup (legacy helper - the app registers autostart
:: itself via Settings -> Start with Windows; this stays for a raw source run).
:: Creates a shortcut in the Startup folder.

set "SCRIPT_DIR=%~dp0"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LIA_SHORTCUT=%STARTUP_DIR%\Lia.lnk"
set "LIA_TARGET=%SCRIPT_DIR%run.bat"
set "LIA_WD=%SCRIPT_DIR%"
set "LIA_ICON=%SCRIPT_DIR%lia.ico"

echo  Creating startup shortcut...

:: Absolute powershell path (no cwd binary-planting); every path is passed via
:: an env var and read with $env:, never interpolated into the PowerShell string
:: (a path with a quote/metachar can't break out) - 2026-09-03 audit #7/#13.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut($env:LIA_SHORTCUT); $s.TargetPath = $env:LIA_TARGET; $s.WorkingDirectory = $env:LIA_WD; $s.Description = 'Lia - Local Inference Assistant'; $s.WindowStyle = 7; if (Test-Path $env:LIA_ICON) { $s.IconLocation = $env:LIA_ICON + ',0' }; $s.Save()"

if exist "%LIA_SHORTCUT%" (
    echo  Lia added to Windows startup!
    echo  It will start automatically when you log in.
) else (
    echo  Failed to create startup shortcut.
)
