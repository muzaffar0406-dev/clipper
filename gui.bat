@echo off
setlocal
cd /d "%~dp0"
title clip-watcher Dashboard

set "PYTHON_EXE="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
    set "PATH=%~dp0.venv\Scripts;%PATH%"
) else if exist "clip-watcher\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=clip-watcher\.venv\Scripts\python.exe"
    set "PATH=%~dp0clip-watcher\.venv\Scripts;%PATH%"
) else (
    set "PYTHON_EXE=python"
)

if exist "%~dp0ffmpeg\ffmpeg-9.0.2-essentials_build\bin" (
    set "PATH=%~dp0ffmpeg\ffmpeg-9.0.2-essentials_build\bin;%PATH%"
)

echo Starting clip-watcher Dashboard...
start "" http://localhost:5000
"%PYTHON_EXE%" "%~dp0gui.py"
pause
