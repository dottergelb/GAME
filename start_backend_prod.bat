@echo off
setlocal
cd /d "%~dp0"

python tools\check_env.py --mode backend
if errorlevel 1 exit /b 1

set ALLOW_DEV_AUTH=false
echo Starting backend in PROD mode (ALLOW_DEV_AUTH=false) ...
python -m uvicorn backend.api.main:app --host 0.0.0.0 --port 8000
