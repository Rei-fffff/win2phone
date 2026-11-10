@echo off
setlocal
title Win2Phone
cd /d "%~dp0pc"

set "PYW=.venv\Scripts\pythonw.exe"
set "SLEEP=%SystemRoot%\System32\timeout.exe"

if not exist "%PYW%" goto :noenv

rem Already running? just open the page, never start a second host.
netstat -ano | findstr ":19012" | findstr "LISTENING" >nul
if not errorlevel 1 goto :open

start "" "%PYW%" app.py

set /a n=0
:wait
%SLEEP% /t 1 /nobreak >nul 2>nul
netstat -ano | findstr ":19012" | findstr "LISTENING" >nul
if not errorlevel 1 goto :done
set /a n+=1
if %n% lss 15 goto :wait

echo [error] console did not start. log follows:
echo ------------------------------------------------------------
if exist app.log type app.log
echo ------------------------------------------------------------
pause
exit /b 1

:open
start "" "http://127.0.0.1:19012/"
exit /b 0

:noenv
echo [error] virtualenv missing: %CD%\%PYW%
echo.
echo Run once:
echo     python -m venv .venv
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
exit /b 1

:done
exit /b 0
