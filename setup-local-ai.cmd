@echo off
setlocal
python "%~dp0scripts\setup_local_ai.py" %*
exit /b %ERRORLEVEL%
