[CmdletBinding()]
param([string]$RootPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Assert-AssistInstallation (Resolve-AssistRoot $RootPath)
$statePath = Join-Path $root 'state/server.json'
if (-not (Test-Path -LiteralPath $statePath)) { Write-Host 'No tracked companion server.'; return }
$state = Read-AssistJson $statePath
$identity = $state.identity
if (-not (Test-AssistProcessIdentity $identity)) {
    Remove-Item -LiteralPath $statePath -Force
    Write-Host 'The tracked server identity has ended. Stale tracking was removed; any process reusing its PID was preserved.'; return
}
if (-not (Test-AssistServerIdentity $identity $root)) { throw 'The recorded process is not this companion server; it was preserved.' }
function Get-OwnedChildren {
    $all = @(Get-CimInstance Win32_Process)
    $queue = New-Object 'Collections.Generic.Queue[int]'; $queue.Enqueue([int]$identity.pid)
    $found = @()
    while ($queue.Count -gt 0) {
        $parent = $queue.Dequeue()
        foreach ($child in @($all | Where-Object { $_.ParentProcessId -eq $parent })) {
            $entry = Get-AssistProcessIdentity ([int]$child.ProcessId)
            if ($entry -and [DateTime]::Parse($entry.start_time_utc) -ge [DateTime]::Parse($identity.start_time_utc)) { $found += $entry; $queue.Enqueue([int]$child.ProcessId) }
        }
    }
    return $found
}
$children = @(Get-OwnedChildren)
$health = Get-AssistHealth ([int]$state.port)
if ($health -and $health.service -eq 'osaurus-arm64-assist' -and $health.instance_id -eq $state.instance_id -and [int]$health.pid -eq [int]$identity.pid) {
    $config = Read-AssistJson (Join-Path $root 'config.json')
    try {
        $null = Invoke-AssistLocalJson -Url "http://127.0.0.1:$($state.port)/admin/shutdown" -Method POST -ApiKey $config.api_key -TimeoutSec 3
    } catch { Write-Verbose 'Graceful shutdown request failed; process identity remains the safety boundary.' }
}
$clock = [Diagnostics.Stopwatch]::StartNew()
while ($clock.Elapsed.TotalSeconds -lt 20 -and (Test-AssistProcessIdentity $identity)) { Start-Sleep -Milliseconds 250 }
if (Test-AssistProcessIdentity $identity) { $children += @(Get-OwnedChildren) }
foreach ($child in @($children | Sort-Object pid -Unique)) {
    if (Test-AssistProcessIdentity $child) { Stop-Process -Id ([int]$child.pid) -ErrorAction SilentlyContinue }
}
if (Test-AssistProcessIdentity $identity) { Stop-Process -Id ([int]$identity.pid) -ErrorAction Stop }
if ($state.launcher_identity -and (Test-AssistProcessIdentity $state.launcher_identity)) { Stop-Process -Id ([int]$state.launcher_identity.pid) -ErrorAction SilentlyContinue }
Remove-Item -LiteralPath $statePath -Force
Write-Host 'Companion and its tracked model workers stopped.'
