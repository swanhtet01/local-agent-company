@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "LOCAL_AI_ROOT=%~dp0"
if /I "%~1"=="--supermega" goto supermega_menu

:menu
cls
echo SuperMega Local AI Lab
echo ======================
echo 1. Open local company chat
echo 2. Show company brief and exact next action ^(no model^)
echo 3. Generate product validation dossier and next experiment ^(no model^)
echo 4. Run next product experiment ^(safe memory recovery^)
echo 5. Inspect and review a completed product experiment ^(no model^)
echo 6. Check and package a proven workflow for owner review ^(no model^)
echo 7. Open local coding agent for a project
echo 8. Check local AI readiness
echo 9. Run one ready company mission ^(safe memory recovery^)
echo A. Start local dashboard
echo S. Open SuperMega workbench
echo 0. Exit
echo.
choice /c 123456789AS0 /n /m "Choose 0-9, A, S, or 0: "
if errorlevel 12 exit /b 0
if errorlevel 11 goto supermega_menu
if errorlevel 10 goto dashboard
if errorlevel 9 goto cycle
if errorlevel 8 goto check
if errorlevel 7 goto code
if errorlevel 6 goto offer
if errorlevel 5 goto review
if errorlevel 4 goto experiment_run
if errorlevel 3 goto validation_pack
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

:validation_pack
call "%LOCAL_AI_ROOT%local-ai.cmd" validation-pack
echo.
pause
goto menu

:experiment_run
call "%LOCAL_AI_ROOT%local-ai.cmd" experiment-run --recover-memory
echo.
pause
goto menu

:offer
call "%LOCAL_AI_ROOT%local-ai.cmd" offer-pack
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

:supermega_menu
cls
echo SuperMega AI Workbench
echo ======================
echo 1. Show SuperMega status and exact next action ^(no model^)
echo 2. Refresh changed SuperMega evidence ^(local state only^)
echo 3. Make SuperMega the active execution focus
echo 4. Inspect the exact queue head and blockers ^(no model^)
echo 5. Park an incompatible queue head ^(local, reversible, no model^)
echo 6. Preview a SuperMega team plan ^(no model^)
echo 7. Queue SuperMega work for autopilot ^(no model^)
echo 8. Run one bounded SuperMega team now
echo 9. Check SuperMega coding-agent readiness
echo A. Open SuperMega coding agent
echo B. Preview the next measured product proof ^(no model^)
echo C. Run one measured product proof ^(safe memory recovery^)
echo D. Review a pending product proof ^(human decision^)
echo E. Generate the private validation dossier ^(no model^)
echo 0. Back to main menu
echo.
choice /c 123456789ABCDE0 /n /m "Choose 0-9, A-E, or 0: "
if errorlevel 15 goto menu
if errorlevel 14 goto supermega_dossier
if errorlevel 13 goto supermega_review
if errorlevel 12 goto supermega_prove
if errorlevel 11 goto supermega_proof
if errorlevel 10 goto supermega_code
if errorlevel 9 goto supermega_code_check
if errorlevel 8 goto supermega_work
if errorlevel 7 goto supermega_later
if errorlevel 6 goto supermega_plan
if errorlevel 5 goto supermega_park_next
if errorlevel 4 goto supermega_next
if errorlevel 3 goto supermega_use
if errorlevel 2 goto supermega_refresh
if errorlevel 1 goto supermega_status

:supermega_status
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega
echo.
pause
goto supermega_menu

:supermega_refresh
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega refresh
echo.
pause
goto supermega_menu

:supermega_use
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega use
echo.
pause
goto supermega_menu

:supermega_next
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega next
echo.
pause
goto supermega_menu

:supermega_park_next
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega park-next
echo.
pause
goto supermega_menu

:supermega_proof
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega proof
echo.
pause
goto supermega_menu

:supermega_prove
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega prove
echo.
pause
goto supermega_menu

:supermega_review
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega review
echo.
pause
goto supermega_menu

:supermega_dossier
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega dossier
echo.
pause
goto supermega_menu

:supermega_plan
set "SUPERMEGA_OBJECTIVE="
set /p "SUPERMEGA_OBJECTIVE=Objective to preview: "
if not defined SUPERMEGA_OBJECTIVE goto supermega_menu
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega plan "%SUPERMEGA_OBJECTIVE%"
echo.
pause
goto supermega_menu

:supermega_later
set "SUPERMEGA_OBJECTIVE="
set /p "SUPERMEGA_OBJECTIVE=Objective to queue: "
if not defined SUPERMEGA_OBJECTIVE goto supermega_menu
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega later "%SUPERMEGA_OBJECTIVE%"
echo.
pause
goto supermega_menu

:supermega_work
set "SUPERMEGA_OBJECTIVE="
set /p "SUPERMEGA_OBJECTIVE=Objective to run now: "
if not defined SUPERMEGA_OBJECTIVE goto supermega_menu
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega work "%SUPERMEGA_OBJECTIVE%"
echo.
pause
goto supermega_menu

:supermega_code_check
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega code --check
echo.
pause
goto supermega_menu

:supermega_code
call "%LOCAL_AI_ROOT%local-ai.cmd" supermega code
goto supermega_menu
