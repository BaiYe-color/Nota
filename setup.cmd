@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Nota - Setup

rem Reuse a working environment. This also makes repeated setup runs fast.
set "VENV_VALID=0"
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -V >nul 2>nul
if not errorlevel 1 set "VENV_VALID=1"

if "%VENV_VALID%"=="0" (
  if exist ".venv" (
    echo Existing virtual environment is invalid. Rebuilding it...
    rmdir /s /q ".venv"
  )

  call :find_python
  if not defined PYTHON_EXE (
    echo Python 3.12 was not found. Installing it with Windows Package Manager...
    where winget >nul 2>nul
    if errorlevel 1 goto :no_winget
    winget install --id Python.Python.3.12 --exact --source winget --scope user --accept-source-agreements --accept-package-agreements
    if errorlevel 1 goto :failed
    call :find_python
  )
  if not defined PYTHON_EXE goto :no_python

  echo [1/3] Creating Python virtual environment...
  "%PYTHON_EXE%" -m venv .venv
  if errorlevel 1 goto :failed
)
if not exist ".venv\Scripts\python.exe" goto :no_python

echo.
echo [2/3] Updating pip and installing Nota dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
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

:find_python
set "PYTHON_EXE="
for %%P in ("%LocalAppData%\Programs\Python\Python312\python.exe" "%LocalAppData%\Programs\Python\Python311\python.exe") do (
  if exist "%%~fP" set "PYTHON_EXE=%%~fP"
)
if defined PYTHON_EXE exit /b 0

where py >nul 2>nul
if errorlevel 1 exit /b 0
for /f "delims=" %%P in ('py -3.12 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%P"
if defined PYTHON_EXE exit /b 0
for /f "delims=" %%P in ('py -3.11 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%P"
exit /b 0

:no_winget
echo.
echo Python is missing and Windows Package Manager ^(winget^) is unavailable.
echo Install Python 3.11 or newer from https://www.python.org/downloads/windows/ and run setup.cmd again.
pause
exit /b 1

:no_python
echo.
echo Python could not be prepared. Restart Windows once, then run setup.cmd again.
pause
exit /b 1

:failed
echo.
echo Setup failed. Check your network and terminal output, then retry.
pause
exit /b 1
