@echo off
setlocal
set "LAUNCHPAD_ROOT=%~dp0"

if /I "%~1"=="code" (
  shift
  call "%LAUNCHPAD_ROOT%local-code.cmd" %*
  exit /b %ERRORLEVEL%
)

python "%LAUNCHPAD_ROOT%scripts\local_ai.py" %*
exit /b %ERRORLEVEL%
