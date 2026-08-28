@echo off
:: Create a Lia shortcut on the desktop with the custom icon

set SCRIPT_DIR=%~dp0
set DESKTOP=%USERPROFILE%\Desktop
set SHORTCUT=%DESKTOP%\Lia.lnk
set ICON_PATH=%SCRIPT_DIR%lia.ico

echo  Creating desktop shortcut...

powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT%'); $s.TargetPath = '%SCRIPT_DIR%run.bat'; $s.WorkingDirectory = '%SCRIPT_DIR%'; $s.Description = 'Lia - Local Inference Assistant'; $s.WindowStyle = 7; if (Test-Path '%ICON_PATH%') { $s.IconLocation = '%ICON_PATH%,0' }; $s.Save()"

if exist "%SHORTCUT%" (
    echo  Desktop shortcut created!
    echo  %SHORTCUT%
) else (
    echo  Failed to create desktop shortcut.
)

pause
