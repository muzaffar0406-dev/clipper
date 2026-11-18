<#
.SYNOPSIS
    Launch clip-watcher Web Dashboard
#>
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

$PythonExe = "python"
if (Test-Path "$ScriptDir\.venv\Scripts\python.exe") {
    $PythonExe = "$ScriptDir\.venv\Scripts\python.exe"
    $env:PATH = "$ScriptDir\.venv\Scripts;$env:PATH"
} elseif (Test-Path "$ScriptDir\clip-watcher\.venv\Scripts\python.exe") {
    $PythonExe = "$ScriptDir\clip-watcher\.venv\Scripts\python.exe"
    $env:PATH = "$ScriptDir\clip-watcher\.venv\Scripts;$env:PATH"
}

if (Test-Path "$ScriptDir\ffmpeg\ffmpeg-9.0.2-essentials_build\bin") {
    $env:PATH = "$ScriptDir\ffmpeg\ffmpeg-9.0.2-essentials_build\bin;$env:PATH"
}

Start-Process "http://localhost:5000"
& $PythonExe "$ScriptDir\gui.py"
