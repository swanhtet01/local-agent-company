@echo off
setlocal
set "LAUNCHPAD_ROOT=%~dp0"

python "%LAUNCHPAD_ROOT%scripts\local_ai.py" %*
exit /b %ERRORLEVEL%
