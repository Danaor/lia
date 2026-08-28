@echo off
:: Remove Lia from Windows startup

set SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Lia.lnk

if exist "%SHORTCUT%" (
    del "%SHORTCUT%"
    echo  Lia removed from Windows startup.
) else (
    echo  Lia is not in startup.
)
pause
