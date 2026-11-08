@echo off
rem Win2Phone PC control panel.
rem The embedded Python in this project has no tkinter, so we use the
rem dependency-free local web UI and open it in the default browser.
rem
rem For a native window, run with a standard CPython that has tkinter:
rem     "D:\Program Files\ComfyUI-aki-v3.2\ComfyUI-aki-v3.2\python\python.exe" panel.py
rem
rem For troubleshooting (console output), run:
rem     .venv\Scripts\python.exe panel.py --web --no-browser
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "panel.py" --web
