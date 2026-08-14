# Registers the two real-time listeners as Windows services via NSSM, so
# they start on boot and restart automatically if either one crashes.
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

function Install-ListenerService {
    param([string]$Name, [string]$Module)

    nssm install $Name $PythonExe "-m" $Module
    nssm set $Name AppDirectory $RepoRoot
    # - Restart on any exit, including a clean one - these processes are
    #   meant to run forever; the only reason either exits is a crash inside
    #   its own retry loop giving up, which should not happen given both
    #   modules already retry internally, but the service layer is the
    #   backstop if it ever does.
    nssm set $Name AppExit Default Restart
    nssm set $Name AppRestartDelay 5000
    nssm set $Name Start SERVICE_AUTO_START
    nssm start $Name

    "installed  : $Name"
    "module     : $Module"
    "state      : $(nssm status $Name)"
    ""
}

Install-ListenerService -Name 'HecateNpmListener' -Module 'pipeline.realtime.npm_listener'
Install-ListenerService -Name 'HecateHnListener' -Module 'pipeline.realtime.hn_listener'

"Both services installed. Check status any time with:"
"  nssm status HecateNpmListener"
"  nssm status HecateHnListener"
