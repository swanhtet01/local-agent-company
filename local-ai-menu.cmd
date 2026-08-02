@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "LOCAL_AI_ROOT=%~dp0"

:menu
cls
echo SuperMega Local AI Lab
echo ======================
echo 1. Open local company chat
echo 2. Show company brief and exact next action ^(no model^)
echo 3. Plan next product experiment ^(no model^)
echo 4. Run next product experiment ^(safe memory recovery^)
echo 5. Inspect and review a completed product experiment ^(no model^)
echo 6. Check whether a workflow is ready to package and sell ^(no model^)
echo 7. Open local coding agent for a project
echo 8. Check local AI readiness
echo 9. Run one ready company mission ^(safe memory recovery^)
echo A. Start local dashboard
echo 0. Exit
echo.
choice /c 123456789A0 /n /m "Choose 0-9 or A: "
if errorlevel 11 exit /b 0
if errorlevel 10 goto dashboard
if errorlevel 9 goto cycle
if errorlevel 8 goto check
if errorlevel 7 goto code
if errorlevel 6 goto offer
if errorlevel 5 goto review
if errorlevel 4 goto experiment_run
if errorlevel 3 goto experiment
if errorlevel 2 goto brief
if errorlevel 1 goto company

:company
call "%LOCAL_AI_ROOT%local-company-agent.cmd"
goto menu

:brief
call "%LOCAL_AI_ROOT%local-ai.cmd" brief
echo.
pause
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

:cycle
call "%LOCAL_AI_ROOT%local-ai.cmd" cycle --recover-memory
echo.
pause
goto menu

:dashboard
call "%LOCAL_AI_ROOT%local-ai.cmd" dashboard
echo.
pause
goto menu
