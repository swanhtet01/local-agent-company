@echo off
setlocal

set "OPENCODE_EXE=C:\Users\thesw\tools\node-v24.18.0-win-x64\opencode.cmd"
if not exist "%OPENCODE_EXE%" (
  echo ERROR: OpenCode is not installed at the expected local path.
  echo Run: ollama launch opencode --model qwen3.5:0.8b -y
  exit /b 1
)

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

set "LOCAL_MODEL=qwen3.5:0.8b"
ollama list | findstr /C:"qwen3.5:4b" >nul
if not errorlevel 1 set "LOCAL_MODEL=qwen3.5:4b"

ollama list | findstr /C:"%LOCAL_MODEL%" >nul
if errorlevel 1 (
  echo ERROR: Required local model qwen3.5:0.8b is not installed.
  echo Run: ollama pull qwen3.5:0.8b
  exit /b 4
)

if "%CHECK_ONLY%"=="1" (
  echo READY: SuperMega Local Code Agent can open %TARGET_DIR%
  echo Model: %LOCAL_MODEL% via local Ollama. No paid API required.
  exit /b 0
)

pushd "%TARGET_DIR%" || exit /b 5
echo Starting SuperMega Local Code Agent in %CD%
echo Model: %LOCAL_MODEL% via local Ollama. No paid API required.
call "%OPENCODE_EXE%" . --model ollama/%LOCAL_MODEL%
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
