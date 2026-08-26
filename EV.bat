@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "ROOT_DIR=%CD%"
set "EV_DIR=%ROOT_DIR%\server\main\EV"
set "SERVER_DIR=%ROOT_DIR%\server\main\server"
set "PYTHON=%SERVER_DIR%\.venv\Scripts\python.exe"
set "PATH=%SERVER_DIR%\.venv\Scripts;%PATH%"
set "LOG_DIR=%EV_DIR%\tmp"

if /i "%~1"=="start" (
    set "_EV_NONINTERACTIVE=1"
    goto start
)
if /i "%~1"=="stop" goto stop
if /i "%~1"=="status" goto status

call :is_port_open 8002
if not errorlevel 1 goto running
goto start

:start
if not exist "%PYTHON%" (
    echo [ERROR] Python environment not found:
    echo %PYTHON%
    pause
    exit /b 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
if not exist "%SERVER_DIR%\data" mkdir "%SERVER_DIR%\data"

echo ========================================
echo           EV Project Launcher
echo ========================================
echo.

call :start_go2rtc

call :is_port_open 8002
if errorlevel 1 (
    echo [START] EV backend on port 8002
    pushd "%EV_DIR%"
    start "" /b "%PYTHON%" -X utf8 app.py 1>>"%LOG_DIR%\muse.log" 2>>"%LOG_DIR%\muse.err.log"
    popd
) else (
    echo [READY] EV backend is already running
)

echo.
echo Waiting for EV control plane...
call :wait_for_port 8002 30
if errorlevel 1 (
    echo [WARN] EV did not become ready within 30 seconds.
    echo Check %LOG_DIR%\muse.err.log
)

call :is_port_open 8000
if errorlevel 1 (
    echo [START] Voice core on ports 8000 and 8003
    pushd "%SERVER_DIR%"
    start "" /b "%PYTHON%" -X utf8 app.py 1>>"%LOG_DIR%\core.log" 2>>"%LOG_DIR%\core.err.log"
    popd
) else (
    echo [READY] Voice core is already running
)

echo.
echo Waiting for voice core...
call :wait_for_port 8000 30
if errorlevel 1 (
    echo [WARN] Voice core did not become ready within 30 seconds.
    echo Check %LOG_DIR%\core.err.log
) else (
    echo [READY] http://127.0.0.1:8002
)

echo.
echo [READY] Control UI: http://127.0.0.1:8002/#/terminal/1
echo.
if defined _EV_NONINTERACTIVE exit /b 0
echo EV is running. Keep this window open.
echo IMPORTANT: Pressing Enter will STOP all services.
echo To keep running, just leave this window open (do not press Enter).
set /p "_EV_EXIT=Press Enter only when you want to STOP..."
goto stop

:running
echo EV is already running at http://127.0.0.1:8002/#/terminal/1
echo.
echo IMPORTANT: Pressing Enter will STOP all services.
echo Close this window without pressing Enter if you want to keep it running.
set /p "_EV_EXIT=Press Enter only when you want to STOP..."
goto stop

:stop
echo.
echo [STOP] Stopping the complete project...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='SilentlyContinue';" ^
  "$ports=@(8000,8002,8003,1984);" ^
  "$ids=@(Get-NetTCPConnection -State Listen | Where-Object { $ports -contains $_.LocalPort } | Select-Object -ExpandProperty OwningProcess);" ^
  "$patterns=@('devices\.voice\.terminal','devices\.camera\.terminal','[\\/]EV[\\/]speech[\\/]tts[\\/]worker\.py','[\\/]muse[\\/]speech[\\/]tts[\\/]worker\.py');" ^
  "$managed=Get-CimInstance Win32_Process | Where-Object { $line=$_.CommandLine; $line -and (($patterns | Where-Object { $line -match $_ }).Count -gt 0 -or ($_.ExecutablePath -eq '%PYTHON%' -and $line -match '(^|[ ])app\.py($|[ ])')) } | Select-Object -ExpandProperty ProcessId;" ^
  "$ids=@($ids)+@($managed) | Where-Object { $_ -and $_ -ne $PID } | Sort-Object -Unique;" ^
  "foreach($processId in $ids){ taskkill.exe /PID $processId /T /F 2>$null | Out-Null }"
powershell.exe -NoProfile -Command "Start-Sleep -Seconds 2" >nul 2>&1
echo [DONE] Project stopped.
if /i not "%~1"=="stop" pause
exit /b 0

:status
call :is_port_open 8002
if errorlevel 1 (
    echo EV: stopped
    exit /b 1
)
call :is_port_open 8000
if errorlevel 1 (
    echo EV: control plane online, voice core offline
    exit /b 2
)
echo EV: running at http://127.0.0.1:8002 with voice core
exit /b 0

:start_go2rtc
call :is_port_open 1984
if not errorlevel 1 (
    echo [READY] go2rtc is already running
    exit /b 0
)
if not defined GO2RTC_BIN set "GO2RTC_BIN=C:\Users\Administrator\Desktop\go2rtc.exe"
if not exist "%GO2RTC_BIN%" (
    echo [WARN] go2rtc not found; camera streaming is unavailable.
    echo %GO2RTC_BIN%
    exit /b 0
)
echo [START] go2rtc on port 1984
for %%I in ("%GO2RTC_BIN%") do pushd "%%~dpI"
start "" /b "%GO2RTC_BIN%" 1>>"%LOG_DIR%\go2rtc.log" 2>&1
popd
exit /b 0

:is_port_open
powershell.exe -NoProfile -Command "if(Get-NetTCPConnection -State Listen -LocalPort %~1 -ErrorAction SilentlyContinue){exit 0}else{exit 1}" >nul 2>&1
exit /b %errorlevel%

:wait_for_port
for /l %%I in (1,1,%~2) do (
    call :is_port_open %~1
    if not errorlevel 1 exit /b 0
    powershell.exe -NoProfile -Command "Start-Sleep -Seconds 1" >nul 2>&1
)
exit /b 1
