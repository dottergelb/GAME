@echo off
setlocal
cd /d "%~dp0\miniapp"

where npm >nul 2>nul
if errorlevel 1 (
  echo ERROR: npm not found in PATH. Install Node.js LTS.
  exit /b 1
)

if not exist node_modules (
  echo Installing miniapp dependencies ...
  npm install
  if errorlevel 1 exit /b 1
)

echo Building miniapp ...
npm run build
