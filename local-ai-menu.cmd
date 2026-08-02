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
echo 4. Check whether a workflow is ready to package and sell ^(no model^)
echo 5. Open local coding agent for a project
echo 6. Check local AI readiness
echo 7. Start local dashboard
echo 8. Exit
echo.
choice /c 12345678 /n /m "Choose 1-8: "
if errorlevel 8 exit /b 0
if errorlevel 7 goto dashboard
if errorlevel 6 goto check
if errorlevel 5 goto code
if errorlevel 4 goto offer
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

:offer
call "%LOCAL_AI_ROOT%local-ai.cmd" offer
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
