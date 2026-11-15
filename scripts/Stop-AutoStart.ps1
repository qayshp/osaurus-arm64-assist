[CmdletBinding()]
param([string]$RootPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Assert-AssistInstallation (Resolve-AssistRoot $RootPath)
$operation = Enter-AssistOperationLock $root 'Automatic'
try {
    $statePath = Join-Path $root 'state/supervisor.json'
    $stopPath = Join-Path $root 'state/supervisor-stop.json'
    Assert-AssistPathAncestors $statePath
    Assert-AssistPathAncestors $stopPath
    if (Test-Path -LiteralPath $statePath) {
        $state = Read-AssistJson $statePath
        if (-not (Test-AssistSupervisorIdentity $state $root)) {
            $other = Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
            if ($other -and $other.StartTime.ToFileTimeUtc() -eq [long]$state.creation_filetime) { throw 'Supervisor tracking points to an unverified live process. It was preserved.' }
            Remove-Item -LiteralPath $statePath -Force
        } else {
            $identity = Get-AssistProcessIdentity ([int]$state.pid)
            $children = @(Get-AssistOwnedDescendants $identity)
            Write-AssistJson $stopPath @{instance_id=$state.instance_id}
            $clock = [Diagnostics.Stopwatch]::StartNew()
            while ($clock.Elapsed.TotalSeconds -lt 5 -and (Test-AssistSupervisorIdentity $state $root)) { Start-Sleep -Milliseconds 200 }
            if (Test-AssistSupervisorIdentity $state $root) { $children += @(Get-AssistOwnedDescendants $identity) }
            # The watcher launches only its server/controller descendants, never
            # Osaurus. Capture identities before termination to reject PID reuse.
            foreach ($child in @($children | Sort-Object pid -Unique)) {
                if (Test-AssistProcessIdentity $child) { Stop-Process -Id ([int]$child.pid) -ErrorAction SilentlyContinue }
            }
            if (Test-AssistSupervisorIdentity $state $root) { Stop-Process -Id ([int]$state.pid) -ErrorAction Stop }
            if (Test-Path -LiteralPath $statePath) {
                $current = Read-AssistJson $statePath
                if ($current.instance_id -eq $state.instance_id) { Remove-Item -LiteralPath $statePath -Force }
            }
            if (Test-Path -LiteralPath $stopPath) {
                $request = Read-AssistJson $stopPath
                if ($request.instance_id -eq $state.instance_id) { Remove-Item -LiteralPath $stopPath -Force }
            }
        }
    }
    & (Join-Path $PSScriptRoot 'Stop.ps1') -RootPath $root -ServerOnly -AutomaticOwnerId (Get-AssistInstallationId $root)
    Write-Host 'Automatic watcher and its managed server stopped. The Startup registration was retained.'
} finally { Exit-AssistOperationLock $operation }
