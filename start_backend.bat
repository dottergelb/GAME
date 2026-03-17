@echo off
setlocal
cd /d "%~dp0"

python tools\check_env.py --mode backend
if errorlevel 1 exit /b 1

echo Starting backend on http://127.0.0.1:8000 ...
python -m uvicorn backend.api.main:app --host 0.0.0.0 --port 8000 --reload
