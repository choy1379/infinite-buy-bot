@echo off
cd /d "%~dp0"
call "%~dp0_py.cmd" || (pause & exit /b 1)
call "%~dp0stop.cmd" >nul
if not exist state mkdir state
powershell -NoProfile -ExecutionPolicy Bypass -Command "$q = @(); if ('%~1' -eq 'quiet') { $q = @('--quiet') }; $py = '%PY%'.Split(' '); $a = @($py[1..9] + '-m','laoer','run' + $q) | Where-Object { $_ }; $p = Start-Process -FilePath $py[0] -ArgumentList $a -WorkingDirectory '%~dp0' -WindowStyle Hidden -PassThru; Set-Content -Path '%~dp0state\bot.pid' -Value $p.Id; 'Bot started (PID ' + $p.Id + '). Log: state\bot.log'"
