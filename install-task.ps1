#requires -Version 5.1
<#
.SYNOPSIS
  Install (or update) the daily Windows Task Scheduler entry for OSS Tracker Agent.
.DESCRIPTION
  Creates a daily 1:00 AM Pacific-time trigger that runs run-once.ps1.
  By default $StartTime is interpreted in Pacific time (PST/PDT) and translated
  to the machine's local clock at install time. DST is resolved automatically
  for today's date; re-run this script after each DST transition to keep the
  scheduled local time aligned with Pacific.
.PARAMETER TaskName
  Scheduled-task name. Default: 'OSS-Tracker-Agent'.
.PARAMETER StartTime
  Daily run time (HH:mm). Default '01:00'.
.PARAMETER TimeZone
  How $StartTime is interpreted: 'Pacific' (default, anchors to PST/PDT),
  'Utc', or 'Local' (use the machine's clock as-is).
.EXAMPLE
  .\install-task.ps1                            # 01:00 Pacific
.EXAMPLE
  .\install-task.ps1 -StartTime '02:30'         # 02:30 Pacific
.EXAMPLE
  .\install-task.ps1 -StartTime '09:00' -TimeZone Utc
.EXAMPLE
  .\install-task.ps1 -StartTime '01:00' -TimeZone Local
#>

[CmdletBinding()]
param(
    [string] $TaskName = 'OSS-Tracker-Agent',
    [string] $StartTime = '01:00',
    [ValidateSet('Pacific', 'Utc', 'Local')]
    [string] $TimeZone = 'Pacific'
)

$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
$runOnce     = Join-Path $projectRoot 'run-once.ps1'

if (-not (Test-Path $runOnce)) {
    throw "run-once.ps1 not found at $runOnce"
}

if ($TimeZone -ne 'Local') {
    $parsed = [datetime]::ParseExact($StartTime, 'HH:mm', $null)
    $today  = (Get-Date).Date
    $naive  = $today.Add($parsed.TimeOfDay)

    if ($TimeZone -eq 'Utc') {
        $sourceUtc = [datetime]::SpecifyKind($naive, 'Utc')
    } else {
        try {
            $pacificTz = [System.TimeZoneInfo]::FindSystemTimeZoneById('Pacific Standard Time')
        } catch {
            $pacificTz = [System.TimeZoneInfo]::FindSystemTimeZoneById('America/Los_Angeles')
        }
        $sourceUtc = [System.TimeZoneInfo]::ConvertTimeToUtc(
            [datetime]::SpecifyKind($naive, 'Unspecified'), $pacificTz)
    }

    $local       = $sourceUtc.ToLocalTime()
    $StartTime   = $local.ToString('HH:mm')
    $sourceLabel = if ($TimeZone -eq 'Pacific') { "Pacific $($parsed.ToString('HH:mm'))" } else { "UTC $($parsed.ToString('HH:mm'))" }
    Write-Host "$sourceLabel -> local $StartTime"
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

Write-Host "Registered scheduled task '$TaskName' to run daily at $StartTime ($TimeZone)."
Write-Host ""
Write-Host "Manage:"
Write-Host "  Get-ScheduledTask    -TaskName $TaskName"
Write-Host "  Start-ScheduledTask  -TaskName $TaskName    # run now"
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
