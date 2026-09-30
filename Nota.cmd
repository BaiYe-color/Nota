@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "NOTA_URL=http://127.0.0.1:7860/"
set "LOG_DIR=%~dp0runtime"
set "OUT_LOG=%LOG_DIR%\nota.stdout.log"
set "ERR_LOG=%LOG_DIR%\nota.stderr.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

set "NEED_SETUP=0"
if not exist ".venv\Scripts\python.exe" set "NEED_SETUP=1"
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -V >nul 2>nul
if errorlevel 1 set "NEED_SETUP=1"
if "%NEED_SETUP%"=="1" (
  call "%~dp0setup.cmd"
  if errorlevel 1 exit /b 1
)

rem Reuse a healthy service instead of starting another copy.
powershell -NoProfile -Command "try{$r=Invoke-WebRequest -UseBasicParsing '%NOTA_URL%api/health' -TimeoutSec 2;if($r.StatusCode -eq 200){exit 0};exit 1}catch{exit 1}"
if not errorlevel 1 goto :open

rem Launch independently from this command window and retain diagnostic logs.
wscript.exe //B "%~dp0launch-nota.vbs"
if errorlevel 1 goto :failed

rem Wait for the HTTP health endpoint, not merely an occupied port.
powershell -NoProfile -Command "$d=(Get-Date).AddSeconds(20);while((Get-Date) -lt $d){try{$r=Invoke-WebRequest -UseBasicParsing '%NOTA_URL%api/health' -TimeoutSec 2;if($r.StatusCode -eq 200){exit 0}}catch{};Start-Sleep -Milliseconds 300};exit 1"
if not errorlevel 1 goto :open

:failed
echo.
echo Nota service did not start. Recent error output:
powershell -NoProfile -Command "if(Test-Path '%ERR_LOG%'){Get-Content -LiteralPath '%ERR_LOG%' -Tail 30}else{Write-Output 'No log file was created.'}"
echo.
echo You can also run start.cmd to view the service output directly.
pause
exit /b 1

:open
start "" "%NOTA_URL%"
exit /b 0
