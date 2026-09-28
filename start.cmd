@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please create .venv and install requirements-dev.txt first. See README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" object\server.py
pause
