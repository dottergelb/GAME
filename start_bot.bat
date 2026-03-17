@echo off
setlocal
cd /d "%~dp0"

python tools\check_env.py --mode bot
if errorlevel 1 exit /b 1

echo Starting Telegram bot ...
python bot.py
