@echo off
setlocal

if defined NASS3CP_PYTHON goto check_custom_python

py -3 -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>&1
if not errorlevel 1 goto run_py_launcher

python -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>&1
if not errorlevel 1 goto run_python

python3 -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>&1
if not errorlevel 1 goto run_python3

>&2 echo nass3cp: Python 3.8 or newer was not found.
>&2 echo Install Python from https://www.python.org/downloads/windows/ and enable "Add python.exe to PATH".
exit /b 1

:check_custom_python
"%NASS3CP_PYTHON%" -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>&1
if errorlevel 1 goto invalid_custom_python
"%NASS3CP_PYTHON%" "%~dp0nass3cp" %*
exit /b %errorlevel%

:invalid_custom_python
>&2 echo nass3cp: NASS3CP_PYTHON does not point to Python 3.8 or newer.
exit /b 1

:run_py_launcher
py -3 "%~dp0nass3cp" %*
exit /b %errorlevel%

:run_python
python "%~dp0nass3cp" %*
exit /b %errorlevel%

:run_python3
python3 "%~dp0nass3cp" %*
exit /b %errorlevel%
