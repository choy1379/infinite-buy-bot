@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "function Kill-Tree($id) { Get-CimInstance Win32_Process -Filter \"ParentProcessId=$id\" | ForEach-Object { Kill-Tree $_.ProcessId }; Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }; $f = '%~dp0state\bot.pid'; if (Test-Path $f) { $id = [int](Get-Content $f); Kill-Tree $id; 'Bot stopped (PID ' + $id + ')' } else { 'Bot is not running.' }; $n = 0; Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | Where-Object { $_.CommandLine -match '--url http://localhost:' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }; if ($n) { 'Closed ' + $n + ' leftover tunnel(s)' }"
if exist state\bot.pid del state\bot.pid
exit /b 0
