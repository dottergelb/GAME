@echo off
setlocal
cd /d "%~dp0"

start "Leha Backend" cmd /k start_backend.bat
start "Leha Bot" cmd /k start_bot.bat
start "Leha MiniApp Dev" cmd /k start_miniapp_dev.bat

echo Launched backend, bot and miniapp in separate windows.
