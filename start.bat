@echo off
setlocal
title Win2Phone host
cd /d "%~dp0pc"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [error] virtualenv missing: %CD%\%PY%
  echo.
  echo Create it once:
  echo     python -m venv .venv
  echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

set "SLEEP=%SystemRoot%\System32\timeout.exe"

echo [1/3] USB port forwarding
set "ADB="
where adb >nul 2>nul && set "ADB=adb"
if not defined ADB if exist "D:\Program Files\Android\platform-tools\adb.exe" set "ADB=D:\Program Files\Android\platform-tools\adb.exe"
if defined ADB (
  "%ADB%" reverse tcp:19010 tcp:19010 >nul 2>nul
  "%ADB%" reverse tcp:19011 tcp:19011 >nul 2>nul
  echo       adb reverse 19010 / 19011 done
) else (
  echo       adb not found. For USB mode run these yourself:
  echo           adb reverse tcp:19010 tcp:19010
  echo           adb reverse tcp:19011 tcp:19011
)

echo [2/3] control panel  ^(opens http://127.0.0.1:19012/^)
start "Win2Phone panel" cmd /k %PY% -u panel.py --web
%SLEEP% /t 2 /nobreak >nul 2>nul

echo [3/3] host  --  close this window to stop and remove the virtual display
echo.
%PY% -u host.py --fps 30 --bitrate 14M

echo.
echo Host stopped; the virtual display has been detached.
%SLEEP% /t 15 /nobreak >nul 2>nul
exit /b 0
