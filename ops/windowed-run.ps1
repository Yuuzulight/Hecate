# Hecate windowed run.
#
# Starts Docker Desktop, runs the day's jobs back to back, checks a snapshot
# landed, and shuts Docker down again. The machine does not need to be on at
# any particular time - it needs to be on once a day, whenever that is.
#
# The CronJobs are suspended because they fire on fixed UTC times an hour
# apart, and a window this short would simply miss them. This script creates
# each Job from the same template, so the definitions stay in one place and
# the sequence still runs in the order the schedules encoded.
#
# Measured end to end: about four minutes, against the two and a half hours
# the cluster would have to stay up to catch 02:00 through 04:00 UTC.
#
# Writes one JSON line per run to %LOCALAPPDATA%\Hecate\run-log.jsonl. The
# daily health check reads that rather than the cluster, so it can report on a
# run without starting Docker again.

[CmdletBinding()]
param(
    # - For running it by hand when you want to poke at the cluster afterwards.
    [switch]$KeepDockerRunning,

    # - Bring the cluster up and stop there. For looking at the dashboard or
    #   querying the database after the day's run has already happened and shut
    #   Docker down again. Running the whole thing with -KeepDockerRunning
    #   would work, but it would collect a second time and put a second entry
    #   in the log for a day that only had one real run.
    #
    #   Here rather than in a caller because starting Docker on this machine
    #   means clearing the stale sockets first, and that knowledge should live
    #   in one place.
    [switch]$StartOnly,

    # - Collection takes about three minutes on a cold cache. The ceiling is
    #   for a source hanging, not for normal slowness.
    [int]$JobTimeoutSeconds = 1200
)

# - Deliberately not 'Stop'. Every external call here is checked through
#   $LASTEXITCODE, and `docker info` failing is how this script asks whether
#   Docker is running - it is control flow, not an error. Under 'Stop',
#   PowerShell 5.1 turns a native command's stderr into a terminating error and
#   the script dies on the question instead of answering it. Real failures are
#   raised with an explicit throw, which the try/catch below still catches.
$ErrorActionPreference = 'Continue'

# - Next to the script, not under %LOCALAPPDATA%. AppData is redirected for
#   packaged applications, so a log written there by the scheduled task and a
#   log read there by something else can be two different files that both
#   report the path they were asked for. That cost an hour: the task wrote
#   successfully, said so, and the entry was nowhere to be found.
#
#   A path on the repo drive is the same file for everyone that opens it.
$LogDir  = Join-Path $PSScriptRoot 'logs'
$LogFile = Join-Path $LogDir 'run-log.jsonl'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }

$run = [ordered]@{
    started_at        = (Get-Date).ToString('o')
    finished_at       = $null
    ok                = $false
    docker_was_up     = $false
    docker_stopped    = $false
    stale_sockets_fixed = $false
    jobs              = @()
    snapshot_date     = $null
    snapshot_rows     = $null
    repositories      = $null
    discovered        = $null
    error             = $null
}

function Write-Step($msg) { Write-Host ("[{0:HH:mm:ss}] {1}" -f (Get-Date), $msg) }

function Test-DockerUp {
    docker info 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Test-ClusterUp {
    kubectl get --raw /readyz 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

# - The failure that actually happens, and it happens every time. Docker leaves
#   zero-byte socket stubs behind on shutdown - clean shutdowns included - and
#   on the next start it aborts trying to rebind them: "The file cannot be
#   accessed by the system". Renaming the directory aside is enough; Docker
#   makes a fresh one.
#
#   This runs before every start, not as recovery after one fails. That was
#   tried the other way round and measured: with the stubs left in place the
#   start crashes, puts an error dialog on screen, and costs the four minutes
#   it takes to notice before the retry can clear them and succeed.
#
#   The renamed directories cannot be deleted while Windows still holds the
#   stubs - not by rmdir, .NET, or the \\?\ raw path. Pruning is attempted on
#   each run because they do release eventually, and a failed delete is
#   harmless.
function Clear-StaleSockets {
    $paths = @(
        (Join-Path $env:LOCALAPPDATA 'Docker\run'),
        (Join-Path $env:LOCALAPPDATA 'docker-secrets-engine')
    )

    # - Sweep whatever previous runs left behind. Silent by design: most of
    #   these will refuse, and that is the expected case rather than a problem.
    foreach ($parent in @((Join-Path $env:LOCALAPPDATA 'Docker'), $env:LOCALAPPDATA)) {
        Get-ChildItem $parent -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'run.broken-*' -or $_.Name -like 'docker-secrets-engine.broken-*' } |
            ForEach-Object {
                try { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction Stop } catch { }
            }
    }

    $moved = $false
    foreach ($p in $paths) {
        if (Test-Path $p) {
            $new = "{0}.broken-{1}" -f (Split-Path $p -Leaf), (Get-Date -Format 'yyyyMMddHHmmss')
            try {
                Rename-Item -LiteralPath $p -NewName $new -ErrorAction Stop
                Write-Step "moved stale sockets aside: $new"
                $moved = $true
            } catch {
                Write-Step "could not move $p : $($_.Exception.Message)"
            }
        }
    }
    return $moved
}

# - `docker desktop stop` blocks indefinitely when Docker is in a crashed
#   state, which would hang this script and, once it is on a schedule, leave a
#   task running until someone notices. Give it a deadline and then take the
#   processes down directly. Killing is what orphans the sockets, but the start
#   path clears them before every run, so the cost is already covered.
function Stop-DockerSafely([int]$TimeoutSeconds = 120) {
    $job = Start-Job -ScriptBlock { docker desktop stop }
    if (Wait-Job $job -Timeout $TimeoutSeconds) {
        Receive-Job $job | Out-Null
    } else {
        Write-Step 'stop did not return in time, terminating processes'
        Stop-Job $job -ErrorAction SilentlyContinue
    }
    Remove-Job $job -Force -ErrorAction SilentlyContinue

    Start-Sleep -Seconds 5
    Get-Process -Name '*docker*' -ErrorAction SilentlyContinue | ForEach-Object {
        try { Stop-Process -Id $_.Id -Force -ErrorAction Stop } catch { }
    }
}

function Start-DockerAndWait([int]$TimeoutSeconds = 420) {
    docker desktop start 2>$null | Out-Null
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 10
        if (Test-DockerUp) {
            Write-Step 'docker daemon up, waiting for kubernetes'
            # - The API server trails the daemon by a good margin on a cold start.
            while ((Get-Date) -lt $deadline) {
                if (Test-ClusterUp) { return $true }
                Start-Sleep -Seconds 10
            }
            return $false
        }
    }
    return $false
}

# - Named with a timestamp so a second run in the same day does not collide.
#   The previous run's jobs are pruned at the start rather than the end, so
#   they are still there to inspect if something went wrong overnight.
function Remove-PreviousWindowedJobs {
    $names = kubectl get jobs -n hecate -o "jsonpath={range .items[*]}{.metadata.name}{'\n'}{end}" 2>$null
    foreach ($n in ($names -split "`n")) {
        $n = $n.Trim()
        if ($n -and $n -match '-w\d{12}$') {
            kubectl delete job $n -n hecate --ignore-not-found 2>$null | Out-Null
        }
    }
}

function Invoke-HecateJob($CronJob, $Stamp) {
    $name = "$CronJob-w$Stamp"
    Write-Step "running $CronJob"
    # - Capturing rather than discarding, because "could not create job" reads
    #   identically whether the CronJob was never applied or the cluster went
    #   away mid-run, and those are not the same morning. kubectl's own line
    #   says which. Safe to merge streams here only because
    #   $ErrorActionPreference is Continue - under Stop, 5.1 turns a native
    #   command's stderr into a terminating NativeCommandError.
    $createOutput = kubectl create job $name --from="cronjob/$CronJob" -n hecate 2>&1
    if ($LASTEXITCODE -ne 0) {
        $why = (($createOutput | ForEach-Object { $_.ToString().Trim() }) -join '; ').Trim()
        if ($why.Length -gt 300) { $why = $why.Substring(0, 300) }
        $detail = if ($why) { "could not create job: $why" } else { 'could not create job' }
        return [ordered]@{ job = $CronJob; ok = $false; detail = $detail }
    }

    # - Polled rather than `kubectl wait --for=condition=complete`, which only
    #   ever returns on success or timeout. A job that fails in the first
    #   minute would sit there for the full twenty, and the whole point of a
    #   short window is not spending it waiting for something already dead.
    #
    #   Logs are grabbed the moment failure is seen, because the controller
    #   deletes the pod within a second or two of the backoff limit and after
    #   that there is nothing left to explain the night with.
    $deadline = (Get-Date).AddSeconds($JobTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $succeeded = kubectl get "job/$name" -n hecate -o 'jsonpath={.status.succeeded}' 2>$null
        if ($succeeded -eq '1') {
            return [ordered]@{ job = $CronJob; ok = $true; detail = 'complete' }
        }

        $failed = kubectl get "job/$name" -n hecate -o 'jsonpath={.status.failed}' 2>$null
        if ($failed -and [int]$failed -ge 1) {
            $log = kubectl logs "job/$name" -n hecate --tail=15 2>$null
            $tail = ''
            if ($log) { $tail = ' | ' + (($log | Select-Object -Last 5) -join ' ') }
            return [ordered]@{ job = $CronJob; ok = $false; detail = "failed after $failed attempt(s)$tail" }
        }

        Start-Sleep -Seconds 5
    }

    return [ordered]@{ job = $CronJob; ok = $false; detail = "still running after ${JobTimeoutSeconds}s" }
}

# - The readiness probe is not enough on a cold start. Bringing Docker up
#   re-creates every pod on the node, and `kubectl wait --for=condition=ready`
#   can return against the Ready status the pod object still carries from
#   before that happened. It did: postgres restarted at 07:55:40, the job
#   started 15 seconds later, and the container died on connect twice before
#   hitting its backoff limit.
#
#   So ask the database, rather than asking Kubernetes about the database.
#   A query that answers is the only readiness that matters here.
function Wait-ForDatabase([int]$TimeoutSeconds = 300) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        kubectl exec -n hecate postgres-0 -- psql -U dataflow -d hecate -t -A -c 'SELECT 1' 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

function Get-Scalar($sql) {
    $v = kubectl exec -n hecate postgres-0 -- psql -U dataflow -d hecate -t -A -c $sql 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($v | Select-Object -First 1).Trim()
}

try {
    if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
        throw 'kubectl is not on PATH'
    }

    $run.docker_was_up = Test-DockerUp
    if ($run.docker_was_up) {
        Write-Step 'docker already running, leaving it up afterwards'
    } else {
        # - Before starting, never after failing. See Clear-StaleSockets: the
        #   stubs are left behind by every shutdown and block every start, so
        #   this is the normal path rather than error handling. Safe here
        #   because Docker has just been established to be down.
        $run.stale_sockets_fixed = Clear-StaleSockets

        Write-Step 'starting docker desktop'
        # - Shorter first attempt. A cold start that has not produced a daemon
        #   in four minutes is the crash case, not slowness, and waiting the
        #   full timeout only delays the fix.
        if (-not (Start-DockerAndWait -TimeoutSeconds 240)) {
            # - One retry, because the stale-socket case is both common and
            #   completely fixable. If it fails twice it is something else.
            #
            #   Stop first, then clear. A crashed Docker leaves its error
            #   dialog running, and that process holds the directory open -
            #   renaming it out from under a live process does not work.
            Write-Step 'did not come up; stopping, clearing stale sockets, retrying'
            Stop-DockerSafely
            Start-Sleep -Seconds 8

            $run.stale_sockets_fixed = Clear-StaleSockets
            if (-not (Start-DockerAndWait -TimeoutSeconds 420)) {
                throw 'Docker Desktop did not start, twice'
            }
        }
    }
    Write-Step 'cluster reachable'

    kubectl wait --for=condition=ready pod/postgres-0 -n hecate --timeout=300s 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'postgres pod never became ready' }

    # - And then actually connect, because the line above can be satisfied by a
    #   Ready status left over from before the node re-created the pod.
    if (-not (Wait-ForDatabase)) { throw 'postgres never accepted a connection' }
    Write-Step 'database answering'

    if ($StartOnly) {
        # - Nothing collected, so nothing to log about the day. Marked ok so the
        #   exit code says the cluster came up, which is all that was asked for.
        $run.ok = $true
        Write-Step 'cluster is up and staying up (StartOnly)'
        Write-Step 'dashboard: kubectl port-forward svc/grafana 3000:3000 -n hecate'
        Write-Step 'when finished: docker desktop stop'
        return
    }

    Remove-PreviousWindowedJobs

    $stamp = Get-Date -Format 'yyyyMMddHHmm'

    # - Order matters. Collection writes the rows, dbt rebuilds the models over
    #   them, the backup captures the result. Sunday adds the full refresh so
    #   incremental drift cannot accumulate.
    #
    #   Embedding goes last because it is the only one nothing else waits on:
    #   it writes to Redis, reads rows the others have finished with, and a
    #   slow API call at the end costs nobody the backup.
    $sequence = @('hecate-daily', 'hecate-dbt', 'hecate-backup', 'hecate-embed')
    if ((Get-Date).DayOfWeek -eq 'Sunday') { $sequence += 'hecate-dbt-full' }

    # - Jobs whose failure is recorded but does not make the day a failure.
    #   Similarity is an addition to the context rather than part of it, so a
    #   missing API key should not turn a good collection into a red run - and
    #   should not be silent either, which is why it still lands in the log.
    $optional = @('hecate-embed')

    foreach ($cj in $sequence) {
        $r = Invoke-HecateJob $cj $stamp
        $r.optional = ($optional -contains $cj)
        $run.jobs += $r
        # - Keep going after a failure. A broken dbt run should not cost you
        #   the backup, and the log needs to say what each one did.
        if (-not $r.ok) { Write-Step "  $cj -> $($r.detail)" }
    }

    # - The check that matters. Everything above can report success while the
    #   day is still missing from the history.
    $run.snapshot_date = Get-Scalar 'SELECT max(captured_on) FROM repository_snapshots;'
    $run.snapshot_rows = Get-Scalar 'SELECT count(*) FROM repository_snapshots WHERE captured_on = (SELECT max(captured_on) FROM repository_snapshots);'
    $run.repositories  = Get-Scalar 'SELECT count(*) FROM raw_repositories;'
    $run.discovered    = Get-Scalar "SELECT count(*) FROM raw_repositories WHERE origin = 'discovered';"

    $today = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd')
    $snapshotIsToday = ($run.snapshot_date -eq $today)
    $allJobsOk = -not ($run.jobs | Where-Object { -not $_.ok -and -not $_.optional })

    $run.ok = ($snapshotIsToday -and $allJobsOk)
    if (-not $snapshotIsToday) {
        $run.error = "no snapshot for $today UTC; newest is $($run.snapshot_date)"
    }

    Write-Step ("snapshot {0}, {1} rows, {2} repositories" -f $run.snapshot_date, $run.snapshot_rows, $run.repositories)
}
catch {
    $run.error = $_.Exception.Message
    Write-Step "FAILED: $($run.error)"
}
finally {
    if (-not $run.docker_was_up -and -not $KeepDockerRunning -and -not $StartOnly) {
        Write-Step 'stopping docker desktop'
        Stop-DockerSafely
        $run.docker_stopped = $true
    }

    # - StartOnly collected nothing, so it writes nothing. An entry with no
    #   jobs and no snapshot would read as a run that failed to collect, and
    #   the health check counts entries to find missing days.
    if (-not $StartOnly) {
        $run.finished_at = (Get-Date).ToString('o')
        ($run | ConvertTo-Json -Depth 5 -Compress) | Out-File -FilePath $LogFile -Encoding utf8 -Append
        Write-Step ("done, ok={0}, log -> {1}" -f $run.ok, $LogFile)
    }
}

if (-not $run.ok) { exit 1 }
