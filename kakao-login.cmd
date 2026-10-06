@echo off
cd /d "%~dp0"
call "%~dp0_py.cmd" || (pause & exit /b 1)
%PY% -m laoer kakao-login
pause
