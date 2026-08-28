@echo off
:: Add Lia to Windows startup
:: Creates a shortcut in the Startup folder

set SCRIPT_DIR=%~dp0
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT=%STARTUP_DIR%\Lia.lnk
set ICON_PATH=%SCRIPT_DIR%lia.ico

echo  Creating startup shortcut...

powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT%'); $s.TargetPath = '%SCRIPT_DIR%run.bat'; $s.WorkingDirectory = '%SCRIPT_DIR%'; $s.Description = 'Lia - Local Inference Assistant'; $s.WindowStyle = 7; if (Test-Path '%ICON_PATH%') { $s.IconLocation = '%ICON_PATH%,0' }; $s.Save()"

if exist "%SHORTCUT%" (
    echo  Lia added to Windows startup!
    echo  It will start automatically when you log in.
) else (
    echo  Failed to create startup shortcut.
)
