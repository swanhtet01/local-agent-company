@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "LOCAL_AI_ROOT=%~dp0"

:menu
cls
echo SuperMega Local AI Lab
echo ======================
echo 1. Open local company chat
echo 2. Plan next product experiment ^(no model^)
echo 3. Run next product experiment ^(safe memory recovery^)
echo 4. Open local coding agent for a project
echo 5. Check local AI readiness
echo 6. Start local dashboard
echo 7. Exit
echo.
choice /c 1234567 /n /m "Choose 1-7: "
if errorlevel 7 exit /b 0
if errorlevel 6 goto dashboard
if errorlevel 5 goto check
if errorlevel 4 goto code
if errorlevel 3 goto experiment_run
if errorlevel 2 goto experiment
if errorlevel 1 goto company

:company
call "%LOCAL_AI_ROOT%local-company-agent.cmd"
goto menu

:experiment
call "%LOCAL_AI_ROOT%local-ai.cmd" experiment
echo.
pause
goto menu

:experiment_run
call "%LOCAL_AI_ROOT%local-ai.cmd" experiment-run --recover-memory
echo.
pause
goto menu

:code
set "LOCAL_AI_PROJECT_PATH="
set /p "LOCAL_AI_PROJECT_PATH=Full project folder path: "
if not defined LOCAL_AI_PROJECT_PATH goto menu
call "%LOCAL_AI_ROOT%local-ai.cmd" code "%LOCAL_AI_PROJECT_PATH%"
goto menu

:check
call "%LOCAL_AI_ROOT%local-company-agent.cmd" --check
echo.
pause
goto menu

:dashboard
call "%LOCAL_AI_ROOT%local-ai.cmd" dashboard
echo.
pause
goto menu
