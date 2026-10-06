@echo off
powershell -NoProfile -ExecutionPolicy Bypass -Command "Unregister-ScheduledTask -TaskName 'tecl-infinite-buy-wake' -Confirm:$false -ErrorAction SilentlyContinue; 'Wake task removed'"
pause
