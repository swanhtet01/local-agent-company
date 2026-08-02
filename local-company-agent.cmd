@echo off
setlocal

set "COMPANY_ROOT=%~dp0"
set "OPENCODE_CONFIG=%USERPROFILE%\.config\opencode\opencode.json"

if not exist "%OPENCODE_CONFIG%" (
  echo ERROR: OpenCode configuration was not found: %OPENCODE_CONFIG%
  exit /b 1
)

python -c "import json,sys; value=json.load(open(sys.argv[1],encoding='utf-8')); sys.exit(0 if value.get('mcp',{}).get('local_company',{}).get('enabled') is True and value.get('agent',{}).get('local-company',{}).get('tools',{}).get('local_company_*') is True else 1)" "%OPENCODE_CONFIG%"
if errorlevel 1 (
  echo ERROR: The governed local-company OpenCode profile is missing or disabled.
  exit /b 2
)

if /I "%~1"=="--check" (
  call "%COMPANY_ROOT%local-code.cmd" --company --check "%COMPANY_ROOT%"
  if errorlevel 1 exit /b 3
  echo READY: SuperMega Local Company agent is configured with governed MCP tools.
  echo No model was loaded and no paid API was used.
  exit /b 0
)

if /I "%~1"=="--run" (
  python "%COMPANY_ROOT%scripts\run_local_company_prompt.py" %*
  if errorlevel 2 exit /b 2
  if errorlevel 1 exit /b 1
  exit /b 0
)

call "%COMPANY_ROOT%local-code.cmd" --company "%COMPANY_ROOT%"
exit /b %ERRORLEVEL%
