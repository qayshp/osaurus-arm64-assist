[CmdletBinding()]
param([string]$RootPath, [ValidateRange(1024,65535)][int]$Port = 0, [string]$AutomaticOwnerId)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Assert-AssistInstallation (Resolve-AssistRoot $RootPath)
if ($AutomaticOwnerId -and $AutomaticOwnerId -ine (Get-AssistInstallationId $root)) { throw 'Automatic server owner must match this installation UUID.' }
$operation = Enter-AssistOperationLock $root 'Server'
try {
$configPath = Join-Path $root 'config.json'
Assert-AssistPathAncestors $configPath
$config = Read-AssistJson $configPath
if ($Port -eq 0) { $Port = [int]$config.port }
if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Configure an unprivileged TCP port between 1024 and 65535.' }
$statePath = Join-Path $root 'state/server.json'
Assert-AssistPathAncestors $statePath
Assert-AssistPathAncestors (Join-Path $root 'logs')
if (Test-Path -LiteralPath $statePath) {
    $state = Read-AssistJson $statePath
    if (Test-AssistProcessIdentity $state.identity) {
        if ($AutomaticOwnerId) {
            if (-not $state.PSObject.Properties['automatic_owner_id'] -or $state.automatic_owner_id -ine $AutomaticOwnerId -or
                (Get-Process -Id ([int]$state.identity.pid)).SessionId -ne (Get-Process -Id $PID).SessionId -or
                -not (Test-AssistServerIdentity $state.identity $root)) { throw 'An existing manual, foreign, or other-session server was preserved; automatic adoption is refused.' }
        }
        $health = Get-AssistHealth ([int]$state.port)
        if ($health -and $health.service -eq 'osaurus-arm64-assist' -and $health.instance_id -eq $state.instance_id) {
            Write-Host "Companion is already running on http://127.0.0.1:$($state.port)"; return
        }
        throw 'The tracked companion process is running but unhealthy. Use Stop.ps1 before restarting.'
    }
}
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { throw "Port $Port is in use; the existing listener was preserved." }
$lock = Read-AssistJson (Join-Path $root 'app/runtime.lock.json')
$runtime = Join-Path $root ('runtime/' + $lock.archive.root)
Assert-AssistRuntime $runtime $lock
$python = Join-Path $root '.venv/Scripts/python.exe'
$arguments = @('-m','osaurus_arm64_assist','serve','--config',$configPath,'--host','127.0.0.1','--port',[string]$Port)
$argumentText = (@($arguments | ForEach-Object { Quote-AssistArgument $_ }) -join ' ')
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$stdout = Join-Path $root "logs/server-$stamp.stdout.log"
$stderr = Join-Path $root "logs/server-$stamp.stderr.log"
$launcher = $null; $launcherIdentity = $null; $identity = $null
try {
$launcher = Start-Process -FilePath $python -ArgumentList $argumentText -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
$launcherStart = $launcher.StartTime.ToUniversalTime().ToString('o')
$launcherIdentity = Get-AssistProcessIdentity $launcher.Id
$clock = [Diagnostics.Stopwatch]::StartNew()
$health = $null
while ($clock.Elapsed.TotalSeconds -lt 30) {
    $candidate = Get-AssistHealth $Port
    if ($candidate -and $candidate.service -eq 'osaurus-arm64-assist') { $health = $candidate; break }
    if ($launcher.HasExited) { break }
    Start-Sleep -Milliseconds 250
}
if (-not $health) {
    throw "Companion startup failed. Inspect $stderr"
}
$candidateIdentity = Get-AssistProcessIdentity ([int]$health.pid)
if (-not $candidateIdentity -or $candidateIdentity.command_line -notlike '*osaurus_arm64_assist*' -or
    $candidateIdentity.command_line.IndexOf($configPath, [StringComparison]::OrdinalIgnoreCase) -lt 0) { throw 'The responding process does not match this companion configuration.' }
if ([DateTime]::Parse($candidateIdentity.start_time_utc) -lt [DateTime]::Parse($launcherStart)) { throw 'The responding process predates this launcher.' }
$ancestor = Get-CimInstance Win32_Process -Filter "ProcessId=$($candidateIdentity.pid)"
$descendsFromLauncher = [int]$candidateIdentity.pid -eq $launcher.Id
for ($i=0; $i -lt 8 -and -not $descendsFromLauncher -and $ancestor; $i++) {
    if ([int]$ancestor.ParentProcessId -eq $launcher.Id) { $descendsFromLauncher = $true; break }
    $ancestor = Get-CimInstance Win32_Process -Filter "ProcessId=$($ancestor.ParentProcessId)" -ErrorAction SilentlyContinue
}
if (-not $descendsFromLauncher) { throw 'The responding server is not a descendant of the new launcher.' }
$identity = $candidateIdentity
$listener = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if (-not ($listener | Where-Object { $_.OwningProcess -eq $identity.pid -and $_.LocalAddress -eq '127.0.0.1' })) { throw 'Listener identity did not match the local companion.' }
$serverState = @{port=$Port;instance_id=$health.instance_id;identity=$identity;launcher_identity=$launcherIdentity;stdout=$stdout;stderr=$stderr;root=$root}
if ($AutomaticOwnerId) { $serverState.automatic_owner_id=$AutomaticOwnerId }
Write-AssistJson $statePath $serverState
Write-Host "Companion started on http://127.0.0.1:$Port (PID $($identity.pid)). Models load on demand."
} catch {
    $launcherCurrent = $false
    if ($launcherIdentity) { $launcherCurrent = Test-AssistProcessIdentity $launcherIdentity }
    $launcherGone = $launcher -and -not (Get-Process -Id $launcher.Id -ErrorAction SilentlyContinue)
    if ($launcherCurrent -or $launcherGone) {
        $all = @(Get-CimInstance Win32_Process)
        $queue = New-Object 'Collections.Generic.Queue[int]'; $queue.Enqueue($launcher.Id)
        $ownedChildren = @()
        while ($queue.Count -gt 0) {
            $parent = $queue.Dequeue()
            foreach ($child in @($all | Where-Object { $_.ParentProcessId -eq $parent })) {
                if ($launcherGone -and $parent -eq $launcher.Id -and ($child.CommandLine -notlike '*osaurus_arm64_assist*' -or $child.CommandLine.IndexOf($configPath,[StringComparison]::OrdinalIgnoreCase) -lt 0)) { continue }
                $entry = Get-AssistProcessIdentity ([int]$child.ProcessId)
                if ($entry -and [DateTime]::Parse($entry.start_time_utc) -ge [DateTime]::Parse($launcherStart)) { $ownedChildren += $entry; $queue.Enqueue([int]$child.ProcessId) }
            }
        }
        foreach ($child in $ownedChildren) { if (Test-AssistProcessIdentity $child) { Stop-Process -Id ([int]$child.pid) -ErrorAction SilentlyContinue } }
        if ($identity -and (Test-AssistProcessIdentity $identity)) { Stop-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue }
        if ($launcherCurrent) { Stop-Process -Id $launcher.Id -ErrorAction SilentlyContinue }
    } elseif ($launcher -and -not $launcher.HasExited) {
        if ($identity -and (Test-AssistProcessIdentity $identity)) { Stop-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue }
        $current = Get-Process -Id $launcher.Id -ErrorAction SilentlyContinue
        if ($current -and $current.StartTime.ToUniversalTime().ToString('o') -eq $launcherStart) { Stop-Process -Id $launcher.Id -ErrorAction SilentlyContinue }
    }
    throw
}
} finally { Exit-AssistOperationLock $operation }
