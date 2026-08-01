@echo off
setlocal

set "OPENCODE_EXE=C:\Users\thesw\tools\node-v24.18.0-win-x64\opencode.cmd"
if not exist "%OPENCODE_EXE%" (
  echo ERROR: OpenCode is not installed at the expected local path.
  echo Run: ollama launch opencode --model qwen3.5:0.8b -y
  exit /b 1
)

if /I "%~1"=="--run" goto RUN_HEADLESS

set "CHECK_ONLY=0"
set "TARGET_DIR=%CD%"
if /I "%~1"=="--check" (
  set "CHECK_ONLY=1"
  if not "%~2"=="" set "TARGET_DIR=%~f2"
) else (
  if not "%~1"=="" set "TARGET_DIR=%~f1"
)
if not exist "%TARGET_DIR%\." (
  echo ERROR: Project directory not found: %TARGET_DIR%
  exit /b 2
)

where ollama >nul 2>nul
if errorlevel 1 (
  echo ERROR: Ollama is not available on PATH.
  exit /b 3
)

set "LOCAL_MODEL="
for /f "usebackq delims=" %%M in (`python "%~dp0scripts\select_local_code_model.py" --model-only`) do set "LOCAL_MODEL=%%M"
if errorlevel 1 exit /b 4
if not defined LOCAL_MODEL (
  echo ERROR: Local coding model selection returned no model.
  exit /b 4
)

if "%CHECK_ONLY%"=="1" (
  echo READY: Local AI Code Agent can open %TARGET_DIR%
  echo Model: %LOCAL_MODEL% via local Ollama. No paid API required.
  exit /b 0
)

pushd "%TARGET_DIR%" || exit /b 5
echo Starting Local AI Code Agent in %CD%
echo Model: %LOCAL_MODEL% via local Ollama. No paid API required.
call "%OPENCODE_EXE%" . --model ollama/%LOCAL_MODEL%
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%

:RUN_HEADLESS
python "%~dp0scripts\run_local_code_agent.py" %*
exit /b %ERRORLEVEL%
