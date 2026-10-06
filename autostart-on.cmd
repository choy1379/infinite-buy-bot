@echo off
rem Starts the bot automatically when you log in to Windows (no admin rights needed).
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\tecl-infinite-buy.cmd"
> "%LNK%" echo @call "%~dp0start.cmd"
echo Autostart enabled: %LNK%
pause
