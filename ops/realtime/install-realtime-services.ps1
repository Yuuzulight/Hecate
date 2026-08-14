# Registers the two real-time listeners as Windows services via NSSM, so
# they start on boot and restart automatically if either one crashes, with
# their stdout/stderr captured to ops/logs/ - the established convention
# this project already uses for windowed-run.ps1's run-log.jsonl - so a
# misconfiguration (e.g. a missing REDIS_REALTIME_URL raising SystemExit on
# startup) or anything either listener logs is actually diagnosable rather
# than going nowhere.
#
# Run it once. Re-running is safe; it replaces the existing services.
#
#   powershell -ExecutionPolicy Bypass -File ops/realtime/install-realtime-services.ps1
#
# To remove them:
#
#   nssm remove HecateNpmListener confirm
#   nssm remove HecateHnListener confirm

[CmdletBinding()]
param(
    [string]$PythonExe = (Get-Command python).Source,
    [string]$RepoRoot = (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent)
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    throw "nssm not found on PATH. Install it first (e.g. 'winget install nssm' or download from nssm.cc)."
}

# - Same directory windowed-run.ps1 already writes run-log.jsonl to, so
#   there is one obvious place to look for anything this project logs to a
#   file, not two.
$LogDir = Join-Path $RepoRoot 'ops\logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }

function Install-ListenerService {
    param([string]$Name, [string]$Module)

    # - "-u": Python block-buffers stdout by default once it isn't a
    #   terminal (i.e. whenever NSSM redirects it to a file, below) - without
    #   this, log lines sit in an internal buffer and only appear once it
    #   fills or the process exits, which defeats the AppStdout/AppStderr
    #   logging below at exactly the moment it matters most (watching a
    #   listener that's actively failing).
    nssm install $Name $PythonExe "-u" "-m" $Module
    nssm set $Name AppDirectory $RepoRoot
    # - Restart on any exit, including a clean one - these processes are
    #   meant to run forever; the only reason either exits is a crash inside
    #   its own retry loop giving up, which should not happen given both
    #   modules already retry internally, but the service layer is the
    #   backstop if it ever does.
    nssm set $Name AppExit Default Restart
    nssm set $Name AppRestartDelay 5000
    nssm set $Name Start SERVICE_AUTO_START

    # - Without this, everything either listener prints - including the
    #   SystemExit("REDIS_REALTIME_URL is required...") misconfiguration
    #   message and every log.warning the bus/listeners emit - goes nowhere
    #   and cannot be inspected. One log file per service, rotated so a
    #   listener that runs for months doesn't grow it without bound.
    $LogFile = Join-Path $LogDir "$Name.log"
    nssm set $Name AppStdout $LogFile
    nssm set $Name AppStderr $LogFile
    nssm set $Name AppRotateFiles 1
    nssm set $Name AppRotateBytes 10485760
    # - NSSM only rotates at service start by default. These services are
    #   designed to never exit (AppExit Default Restart above), so without
    #   this the rotate settings above are silently inert for a listener
    #   that just keeps running - AppRotateOnline is what actually rotates
    #   the file while the service stays up.
    nssm set $Name AppRotateOnline 1

    nssm start $Name

    "installed  : $Name"
    "module     : $Module"
    "log        : $LogFile"
    "state      : $(nssm status $Name)"
    ""
}

Install-ListenerService -Name 'HecateNpmListener' -Module 'pipeline.realtime.npm_listener'
Install-ListenerService -Name 'HecateHnListener' -Module 'pipeline.realtime.hn_listener'

"Both services installed. Check status any time with:"
"  nssm status HecateNpmListener"
"  nssm status HecateHnListener"
