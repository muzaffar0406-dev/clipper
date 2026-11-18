@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    echo Usage: %~nx0 ^<twitch_channel^> [--min N] [options]
    echo Example: %~nx0 shroud --min 5
    exit /b 1
)

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

if not exist "rec" mkdir "rec"
if not exist "clips" mkdir "clips"

"%PYTHON_EXE%" "%~dp0watcher.py" %*
