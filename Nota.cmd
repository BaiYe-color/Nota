@echo off
setlocal
cd /d "%~dp0"
set "NOTA_URL=http://127.0.0.1:7860/"

set "NEED_SETUP=0"
if not exist ".venv\Scripts\python.exe" set "NEED_SETUP=1"
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -V >nul 2>nul
if errorlevel 1 set "NEED_SETUP=1"
if "%NEED_SETUP%"=="1" (
  call "%~dp0setup.cmd"
  if errorlevel 1 exit /b 1
)

rem If the service is already responding, just open the browser.
powershell -NoProfile -Command "try{$c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',7860); $c.Close(); exit 0}catch{exit 1}"
if not errorlevel 1 goto :open

rem Clear any stale process still holding port 7860 so a fresh one can bind.
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"

rem Start the service in the background, sharing this console's handles.
rem The /b flag is required: without it pythonw.exe gets no valid stderr
rem handle and uvicorn crashes immediately (silently, since it has no window).
start "" /b ".venv\Scripts\pythonw.exe" "object\server.py"

rem Wait up to 20 seconds, polling fast from a single process.
powershell -NoProfile -Command "$d=(Get-Date).AddSeconds(20); while((Get-Date) -lt $d){ try{$c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',7860); $c.Close(); exit 0}catch{Start-Sleep -Milliseconds 300} }; exit 1"
if not errorlevel 1 goto :open

echo.
echo Nota service did not start within 20 seconds.
echo Run start.cmd in a terminal to see the actual error.
echo.
pause
exit /b 1

:open
start "" "%NOTA_URL%"
exit /b 0
