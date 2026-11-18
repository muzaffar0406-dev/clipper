<#
.SYNOPSIS
    clip-watcher PowerShell launcher
.EXAMPLE
    .\watch.ps1 shroud
    .\watch.ps1 tarik --min 5 --quality 720p60
#>
[CmdletBinding()]
param(
    [Parameter(Position=0, Mandatory=$true, HelpMessage="Twitch channel name (e.g. shroud, tarik)")]
    [string]$Channel,
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$RemainingArgs
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# Auto-detect Python (prefer virtualenv)
$PythonCandidates = @(
    "$ScriptDir\.venv\Scripts\python.exe",
    "$ScriptDir\clip-watcher\.venv\Scripts\python.exe",
    "python",
    "python3"
)

$PythonExe = ""
foreach ($cand in $PythonCandidates) {
    if (Test-Path $cand) {
        $PythonExe = $cand
        break
    } elseif (Get-Command $cand -ErrorAction SilentlyContinue) {
        $PythonExe = $cand
        break
    }
}

if (-not $PythonExe) {
    Write-Error "Python not found. Please ensure Python is installed or virtualenv exists."
    exit 1
}

# Auto-add local ffmpeg and venv scripts to PATH
$ExtraPaths = @(
    "$ScriptDir\ffmpeg\ffmpeg-9.0.2-essentials_build\bin",
    "$ScriptDir\..\ffmpeg\ffmpeg-9.0.2-essentials_build\bin",
    "$ScriptDir\.venv\Scripts",
    "$ScriptDir\clip-watcher\.venv\Scripts"
)
foreach ($p in $ExtraPaths) {
    if (Test-Path $p) {
        $env:PATH = "$p;$env:PATH"
    }
}

New-Item -ItemType Directory -Force -Path "rec", "clips" | Out-Null
& $PythonExe "$ScriptDir\watcher.py" $Channel @RemainingArgs
