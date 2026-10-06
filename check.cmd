@echo off
cd /d "%~dp0"
call "%~dp0_py.cmd" || (pause & exit /b 1)
echo === notify-test ===
%PY% -m laoer notify-test
echo.
echo === status ===
%PY% -m laoer status
echo.
echo === plan (next session preview, no orders) ===
%PY% -m laoer plan
pause
