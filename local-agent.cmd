@echo off
setlocal
set "AGENT_ROOT=%~dp0"
set "PYTHONPATH=%AGENT_ROOT%src"

python -m local_company.agent_api %*
exit /b %ERRORLEVEL%
