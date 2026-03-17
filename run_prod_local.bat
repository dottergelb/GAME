@echo off
setlocal
cd /d "%~dp0"

start "Leha Backend PROD" cmd /k start_backend_prod.bat
start "Leha Bot" cmd /k start_bot.bat

echo Launched backend (prod mode) and bot in separate windows.
