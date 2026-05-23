#requires -Version 5.1
<#
.SYNOPSIS
  Install (or update) the daily Windows Task Scheduler entry for OSS Tracker Agent.
.DESCRIPTION
  Creates a daily 1:00 AM trigger that runs run-once.ps1.
  The trigger uses LOCAL Windows time — confirm your system timezone is
  Pacific (PST/PDT). To force a UTC time instead, pass -StartTimeUtc.
.PARAMETER TaskName
  Scheduled-task name. Default: 'OSS-Tracker-Agent'.
.PARAMETER StartTime
  Local time to run daily (HH:mm). Default '01:00'.
.PARAMETER StartTimeUtc
  If set, $StartTime is interpreted as UTC and translated to local.
.EXAMPLE
  .\install-task.ps1
.EXAMPLE
  .\install-task.ps1 -StartTime '09:00' -StartTimeUtc
#>

[CmdletBinding()]
param(
    [string] $TaskName = 'OSS-Tracker-Agent',
    [string] $StartTime = '01:00',
    [switch] $StartTimeUtc
)

$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
$runOnce     = Join-Path $projectRoot 'run-once.ps1'

if (-not (Test-Path $runOnce)) {
    throw "run-once.ps1 not found at $runOnce"
}

if ($StartTimeUtc) {
    $utc   = [datetime]::ParseExact($StartTime, 'HH:mm', $null)
    $today = (Get-Date).Date
    $utcAt = [datetime]::SpecifyKind($today.Add($utc.TimeOfDay), 'Utc')
    $local = $utcAt.ToLocalTime()
    $StartTime = $local.ToString('HH:mm')
    Write-Host "UTC $($utcAt.ToString('HH:mm')) -> local $StartTime"
}

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runOnce`""

$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Daily OSS PR tracker + auto-actions + email digest." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' to run daily at $StartTime."
Write-Host ""
Write-Host "Manage:"
Write-Host "  Get-ScheduledTask    -TaskName $TaskName"
Write-Host "  Start-ScheduledTask  -TaskName $TaskName    # run now"
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
