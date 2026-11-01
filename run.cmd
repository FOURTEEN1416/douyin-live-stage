@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ---- pick the Python that actually has windows_capture installed ----
REM The system python (C:\Program Files\Python312) does NOT have it.
set "PY="
if exist "C:\Users\FOUR\.workbuddy\binaries\python\versions" (
  for /f "delims=" %%i in ('dir /b /o-n "C:\Users\FOUR\.workbuddy\binaries\python\versions" 2^>nul') do (
    if not defined PY if exist "C:\Users\FOUR\.workbuddy\binaries\python\versions\%%i\python.exe" set "PY=C:\Users\FOUR\.workbuddy\binaries\python\versions\%%i\python.exe"
  )
)
if not defined PY set "PY=python"

echo ============================================================
echo  Live Stage - FULL AUTO
echo  python : %PY%
echo ============================================================
echo.
echo  Stage  : http://127.0.0.1:8790/stage.html
echo  Panel  : http://127.0.0.1:8790/panel.html
echo.
echo  Starting: server + compositor + overlay + narrator
echo  Keep this window open while streaming. Ctrl+C to stop.
echo.

start "" "http://127.0.0.1:8790/panel.html"
"%PY%" -u run.py --mode auto --noframes
pause
