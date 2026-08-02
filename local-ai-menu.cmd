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
echo 4. Inspect and review a completed product experiment ^(no model^)
echo 5. Check whether a workflow is ready to package and sell ^(no model^)
echo 6. Open local coding agent for a project
echo 7. Check local AI readiness
echo 8. Start local dashboard
echo 9. Exit
echo.
choice /c 123456789 /n /m "Choose 1-9: "
if errorlevel 9 exit /b 0
if errorlevel 8 goto dashboard
if errorlevel 7 goto check
if errorlevel 6 goto code
if errorlevel 5 goto offer
if errorlevel 4 goto review
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

:review
call "%LOCAL_AI_ROOT%local-ai.cmd" experiment-review-interactive
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
