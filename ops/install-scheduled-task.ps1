# Registers the Windows scheduled task that runs windowed-run.ps1 once a day.
#
# Run it once. Re-running is safe; it replaces the existing task.
#
#   powershell -ExecutionPolicy Bypass -File ops/install-scheduled-task.ps1
#
# To remove it:
#
#   Unregister-ScheduledTask -TaskName 'Hecate daily run' -Confirm:$false

[CmdletBinding()]
param(
    # - 10:00 local. The constraint is not the hour, it is the UTC date: this
    #   machine is UTC+8, so anything from 08:00 local onward lands on the
    #   current UTC day, which is what captured_on is keyed on. Running before
    #   08:00 would write to the previous UTC date and read as a missed day.
    [string]$At = '10:00',
    [string]$TaskName = 'Hecate daily run'
)

$ErrorActionPreference = 'Stop'

$script = Join-Path (Split-Path $PSScriptRoot -Parent) 'ops\windowed-run.ps1'
if (-not (Test-Path $script)) { throw "cannot find $script" }

# - Wrapped in cmd so stdout and stderr land in a file. A scheduled task runs
#   with no console, and the JSON log is only written at the very end - so a
#   run that hangs or is killed leaves nothing at all behind. That happened,
#   and cost an hour of guessing at a task that reported success. This file is
#   overwritten each run and shows how far the last one got, live.
#   powershell.exe is deliberately unquoted. When the string after /c starts
#   with a quote, cmd strips the outer pair and the rest of the command comes
#   apart - the task exits 1 having run nothing and created no trace file,
#   which looks exactly like the script failing instantly.
$trace = Join-Path (Split-Path $script -Parent) 'logs\last-run.txt'
$inner = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $script

$action = New-ScheduledTaskAction `
    -Execute 'cmd.exe' `
    -Argument ('/c {0} > "{1}" 2>&1' -f $inner, $trace)

$trigger = New-ScheduledTaskTrigger -Daily -At $At

# - StartWhenAvailable is the point of the whole arrangement: if the machine is
#   off at 10:00, the run happens when it next comes on rather than being
#   skipped. A skipped day is a permanent hole in the snapshot history.
#
#   The battery settings matter on a laptop - by default Windows will not start
#   a task on battery and will stop one already running if you unplug.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

# - Interactive, and not "run whether user is logged on or not". Docker Desktop
#   is a desktop application and will not start in a session-0 service context;
#   the task would run, fail to get a daemon, and log a missed day every time.
$principal = New-ScheduledTaskPrincipal `
    -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Starts Docker Desktop, runs the Hecate collection, dbt rebuild and backup, verifies a snapshot landed, then shuts Docker down again.' `
    -Force | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
$i = Get-ScheduledTaskInfo -TaskName $TaskName
"registered : {0}" -f $t.TaskName
"state      : {0}" -f $t.State
"next run   : {0}" -f $i.NextRunTime
"script     : {0}" -f $script
""
"Run it now with: Start-ScheduledTask -TaskName '$TaskName'"
