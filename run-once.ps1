#requires -Version 5.1
<#
.SYNOPSIS
  One-shot wrapper invoked by Windows Task Scheduler.
.DESCRIPTION
  Activates the venv and runs the OSS Tracker Agent in CLI mode.
  Output is teed to a daily log file under .\logs\.
#>

$ErrorActionPreference = 'Stop'

$ProjectRoot = $PSScriptRoot
$VenvPython  = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$LogDir      = Join-Path $ProjectRoot 'logs'

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$LogFile = Join-Path $LogDir ("{0}.log" -f (Get-Date -Format 'yyyy-MM-dd_HHmm'))

if (-not (Test-Path $VenvPython)) {
    "[$(Get-Date -Format o)] venv python not found at $VenvPython" |
        Tee-Object -FilePath $LogFile | Write-Output
    exit 1
}

Push-Location $ProjectRoot
try {
    "[$(Get-Date -Format o)] starting oss_tracker_agent --cli" |
        Tee-Object -FilePath $LogFile -Append | Write-Output

    & $VenvPython -m oss_tracker_agent.main --cli 2>&1 |
        Tee-Object -FilePath $LogFile -Append

    $exit = $LASTEXITCODE
    "[$(Get-Date -Format o)] exit=$exit" |
        Tee-Object -FilePath $LogFile -Append | Write-Output
    exit $exit
}
finally {
    Pop-Location
}
