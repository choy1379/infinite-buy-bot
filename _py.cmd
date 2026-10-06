@rem Finds a Python 3.11+ interpreter and sets %PY% (used by the other .cmd files).
@set "PY="
@where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3"
@if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python"
@if not defined PY (
  echo [!] Python 3.11 or newer was not found.
  echo     Install it from https://www.python.org/downloads/  and check "Add python.exe to PATH".
  exit /b 1
)
@exit /b 0
