@echo off
cd /d "%~dp0"
call "%~dp0_py.cmd" || (pause & exit /b 1)
echo Python: %PY%
if not exist config.toml (
  copy config.example.toml config.toml >nul
  echo Created config.toml from config.example.toml
)
echo Running self-tests...
%PY% -m unittest discover -s tests -t . >"%TEMP%\laoer-test.log" 2>&1 && (echo Tests OK) || (echo [!] Tests failed - see %TEMP%\laoer-test.log & pause & exit /b 1)
echo.
echo Next: fill in config.toml (Toss keys, capital_usd, Discord webhook / Kakao key).
echo Then run:  check.cmd   and   start.cmd
start "" notepad config.toml
pause
