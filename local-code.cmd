@echo off
setlocal

set "OPENCODE_AGENT="
if /I "%~1"=="--company" (
  set "OPENCODE_AGENT=local-company"
  shift
)
if /I "%~1"=="--vision-lite" (
  set "OPENCODE_AGENT=vision-campaign"
  shift
)

set "OPENCODE_EXE=%LOCAL_OPENCODE%"
if not defined OPENCODE_EXE for %%I in (opencode.cmd) do set "OPENCODE_EXE=%%~$PATH:I"
if not defined OPENCODE_EXE if defined APPDATA set "OPENCODE_EXE=%APPDATA%\npm\opencode.cmd"
if not exist "%OPENCODE_EXE%" (
  echo ERROR: OpenCode was not found in LOCAL_OPENCODE, PATH, or APPDATA\npm.
  echo Run: ollama launch opencode --model llama3.2:1b -y
  exit /b 1
)

if /I "%~1"=="--run" goto RUN_HEADLESS
if /I "%~1"=="--vision" goto RUN_VISION
if /I "%~1"=="--lmstudio" goto RUN_LMSTUDIO

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

set "OLLAMA_EXE="
for %%I in (ollama.exe) do set "OLLAMA_EXE=%%~$PATH:I"
if not exist "%OLLAMA_EXE%" (
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
  if defined OPENCODE_AGENT echo Agent: %OPENCODE_AGENT%
  exit /b 0
)

pushd "%TARGET_DIR%" || exit /b 5
echo Starting Local AI Code Agent in %CD%
echo Model: %LOCAL_MODEL% via local Ollama. No paid API required.
if defined OPENCODE_AGENT (
  call "%OPENCODE_EXE%" . --model ollama/%LOCAL_MODEL% --agent "%OPENCODE_AGENT%"
) else (
  call "%OPENCODE_EXE%" . --model ollama/%LOCAL_MODEL%
)
set "EXIT_CODE=%ERRORLEVEL%"
"%OLLAMA_EXE%" stop "%LOCAL_MODEL%" >nul 2>nul
if errorlevel 1 echo WARNING: Ollama could not unload %LOCAL_MODEL%; run ollama stop %LOCAL_MODEL%.
popd
exit /b %EXIT_CODE%

:RUN_HEADLESS
python "%~dp0scripts\run_local_code_agent.py" %*
exit /b %ERRORLEVEL%

:RUN_VISION
set "VISION_TARGET=%CD%"
if /I "%~2"=="--check" (
  if not "%~3"=="" set "VISION_TARGET=%~f3"
  python "%~dp0scripts\run_lmstudio_code.py" --check "%VISION_TARGET%" --agent vision-product
) else (
  if not "%~2"=="" set "VISION_TARGET=%~f2"
  python "%~dp0scripts\run_lmstudio_code.py" "%VISION_TARGET%" --agent vision-product
)
exit /b %ERRORLEVEL%

:RUN_LMSTUDIO
python "%~dp0scripts\run_lmstudio_code.py" %*
exit /b %ERRORLEVEL%
