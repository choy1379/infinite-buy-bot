@echo off
rem Wakes the PC from sleep at order/report times and (re)starts the bot.
rem 22:50 / 05:30 = US daylight time, 23:50 / 06:30 = US standard time (KST).
cd /d "%~dp0"
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 >nul 2>nul
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 >nul 2>nul
powercfg /setactive SCHEME_CURRENT >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c \"%~dp0start.cmd\" quiet' -WorkingDirectory '%~dp0';" ^
  "$t = @('22:50','23:50','05:30','06:30') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ };" ^
  "$s = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5);" ^
  "Register-ScheduledTask -TaskName 'tecl-infinite-buy-wake' -Action $a -Trigger $t -Settings $s -Force | Out-Null;" ^
  "'Wake task registered: 22:50, 23:50, 05:30, 06:30 daily'"
pause
