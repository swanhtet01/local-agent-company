@echo off
set "PYTHONPATH=%~dp0src"
python -m local_company.cli %*

