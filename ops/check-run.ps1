# Verifies the daily pipeline is firing and producing snapshots without gaps.
# Reads ops/logs/run-log.jsonl (the pipeline's own record) rather than re-deriving state elsewhere.
# Naive per-day string scan over the whole log; fine at ~1 entry/day, revisit if this
# grows into the thousands of lines.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$logPath = Join-Path $root 'ops\logs\run-log.jsonl'
$alertPath = Join-Path $root 'ops\logs\ALERT.txt'
$checkLogPath = Join-Path $root 'ops\logs\check-log.jsonl'

$entries = Get-Content $logPath | ForEach-Object { $_ | ConvertFrom-Json }
$snapshotDates = $entries | Where-Object { $_.snapshot_date } | Select-Object -ExpandProperty snapshot_date -Unique | Sort-Object
$latest = $entries | Select-Object -Last 1

$today = (Get-Date).ToUniversalTime().Date
$expected = $today.AddDays(-1)  # most recent UTC snapshot date we'd expect to see by now

$missing = @()
if ($snapshotDates.Count -gt 0) {
    $d = [datetime]$snapshotDates[0]
    while ($d -le $expected) {
        if ($snapshotDates -notcontains $d.ToString('yyyy-MM-dd')) { $missing += $d.ToString('yyyy-MM-dd') }
        $d = $d.AddDays(1)
    }
}

$taskInfo = Get-ScheduledTaskInfo -TaskName "Hecate Daily Run"
$taskFired = $taskInfo.LastRunTime -gt (Get-Date).AddHours(-30)

$dbtJob = $latest.jobs | Where-Object { $_.job -eq 'hecate-dbt' } | Select-Object -Last 1
$forecastJob = $latest.jobs | Where-Object { $_.job -eq 'hecate-forecast' } | Select-Object -Last 1
$dbtOk = if ($dbtJob) { $dbtJob.ok } else { $null }
$forecastOk = if ($forecastJob) { $forecastJob.ok } else { $null }

$result = [ordered]@{
    checked_at              = (Get-Date).ToUniversalTime().ToString('o')
    task_fired_recently     = $taskFired
    last_run_ok             = $latest.ok
    latest_snapshot_date    = $snapshotDates[-1]
    missing_snapshot_dates  = $missing
    dbt_ok                  = $dbtOk
    forecast_ok             = $forecastOk
    forecast_detail         = $forecastJob.detail
}

$result | ConvertTo-Json -Compress | Add-Content -Path $checkLogPath

if ($missing.Count -gt 0 -or -not $taskFired -or -not $latest.ok -or $dbtOk -eq $false -or $forecastOk -eq $false) {
    "ALERT $(Get-Date -Format o): missing=$($missing -join ',') task_fired_recently=$taskFired last_run_ok=$($latest.ok) dbt_ok=$dbtOk forecast_ok=$forecastOk" |
        Set-Content -Path $alertPath
} elseif (Test-Path $alertPath) {
    Remove-Item $alertPath
}

$result | ConvertTo-Json
