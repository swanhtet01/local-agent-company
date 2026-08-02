@echo off
setlocal
set "COMPANY_ROOT=%~dp0"
set "COMPANY_PY=%COMPANY_ROOT%.venv\Scripts\python.exe"
if not exist "%COMPANY_PY%" set "COMPANY_PY=python"
set "PYTHONPATH=%COMPANY_ROOT%src;%PYTHONPATH%"
"%COMPANY_PY%" -m local_company.mcp_server
exit /b %ERRORLEVEL%
