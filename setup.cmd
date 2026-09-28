@echo off
setlocal
cd /d "%~dp0"
title Nota - Setup

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating Python virtual environment...
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3.13 -m venv .venv 2>nul || py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul
  ) else (
    python -m venv .venv 2>nul
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo Python 3.11 or newer was not found. Install Python and enable Add Python to PATH.
  pause
  exit /b 1
)

echo.
echo [2/3] Updating pip and installing Nota dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

if not exist "object\.env" (
  copy /y "object\.env.example" "object\.env" >nul
  echo [3/3] Created object\.env. Configure models in Nota before generating notes.
) else (
  echo [3/3] Existing object\.env was kept.
)
echo.
echo Setup complete. Double-click Nota.lnk or Nota.cmd to launch Nota.
pause
exit /b 0

:failed
echo.
echo Setup failed. Check your network, Python installation, and terminal output, then retry.
pause
exit /b 1
