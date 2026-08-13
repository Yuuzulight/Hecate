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
    # - 03:00 local, set deliberately and against the grain of the note below.
    #   The constraint is not the hour, it is the UTC date: this machine is
    #   UTC+8, so anything from 08:00 local onward lands on the current UTC day,
    #   which is what captured_on is keyed on. 03:00 local is 19:00 UTC the
    #   PREVIOUS day, so a run at the scheduled hour writes to yesterday's date.
    #
    #   That on its own is survivable - every UTC date still gets exactly one
    #   snapshot, just captured near the end of it instead of near the start.
    #   The hazard is StartWhenAvailable below. If the machine is asleep at
    #   03:00 the catch-up run happens after wake, which is usually after 08:00
    #   local and therefore lands on the CURRENT UTC date. So which date a run
    #   writes depends on whether the laptop happened to be awake overnight, and
    #   an overnight-on day followed by an overnight-off day skips a UTC date
    #   entirely. That gap is permanent; snapshots cannot be backfilled.
    #
    #   08:00 is the earliest hour that is unconditionally safe.
    [string]$At = '03:00',
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
#   off at 03:00, the run happens when it next comes on rather than being
#   skipped. A skipped day is a permanent hole in the snapshot history.
#   Note this interacts badly with a pre-08:00 hour - see the $At comment.
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
