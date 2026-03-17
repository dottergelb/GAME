@echo off
setlocal
cd /d "%~dp0"

python tools\migrate_sqlite_to_postgres.py
