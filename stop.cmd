@echo off
cd /d "%~dp0"
if not exist state\bot.pid (echo Bot is not running. & exit /b 0)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$id = Get-Content '%~dp0state\bot.pid'; Get-CimInstance Win32_Process -Filter \"ParentProcessId=$id\" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Stop-Process -Id $id -Force -ErrorAction SilentlyContinue; 'Bot stopped (PID ' + $id + ')'"
del state\bot.pid
