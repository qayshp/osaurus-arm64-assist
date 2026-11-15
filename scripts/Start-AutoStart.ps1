[CmdletBinding()]
param([string]$RootPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Assert-AssistInstallation (Resolve-AssistRoot $RootPath)
$operation = Enter-AssistOperationLock $root 'Automatic'
try {
    $configPath = Join-Path $root 'config.json'
    $config = Read-AssistJson $configPath
    if ($config.PSObject.Properties['auto_start'] -and $config.auto_start -eq $false) { throw 'Automatic mode is disabled. Use Enable-AutoStart.ps1 to enable it.' }
    $statePath = Join-Path $root 'state/supervisor.json'
    Assert-AssistPathAncestors $statePath
    if (Test-Path -LiteralPath $statePath) {
        $existing = Read-AssistJson $statePath
        if (Test-AssistSupervisorIdentity $existing $root) { Write-Host 'Automatic lifecycle watcher is already running.'; return }
        $other = Get-Process -Id ([int]$existing.pid) -ErrorAction SilentlyContinue
        if ($other -and $other.StartTime.ToFileTimeUtc() -eq [long]$existing.creation_filetime) { throw 'Supervisor tracking points to an unverified live process. It was preserved.' }
        Remove-Item -LiteralPath $statePath -Force
    }
    $pythonw = Join-Path $root '.venv/Scripts/pythonw.exe'
    Assert-AssistPathAncestors $pythonw
    if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { throw 'Installed Pythonw is missing. Reinstall the companion.' }
    $arguments = @('-I','-m','osaurus_arm64_assist','watch','--config',$configPath)
    $argumentText = (@($arguments | ForEach-Object { Quote-AssistArgument $_ }) -join ' ')
    $launcher = $null; $launcherIdentity = $null; $newState = $null
    try {
        $launcher = Start-Process -FilePath $pythonw -ArgumentList $argumentText -WorkingDirectory $root -WindowStyle Hidden -PassThru
        $launcherIdentity = Get-AssistProcessIdentity $launcher.Id
        $started = $launcher.StartTime.ToFileTimeUtc()
        $clock = [Diagnostics.Stopwatch]::StartNew()
        while ($clock.Elapsed.TotalSeconds -lt 15) {
            if (Test-Path -LiteralPath $statePath) {
                try { $candidate = Read-AssistJson $statePath } catch { $candidate = $null }
                if ($candidate -and (Test-AssistSupervisorIdentity $candidate $root) -and [long]$candidate.creation_filetime -ge $started) {
                    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($candidate.pid)"
                    $descends = [int]$candidate.pid -eq $launcher.Id
                    for ($depth=0; $depth -lt 8 -and -not $descends -and $process; $depth++) {
                        if ([int]$process.ParentProcessId -eq $launcher.Id) { $descends=$true; break }
                        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)" -ErrorAction SilentlyContinue
                    }
                    if ($descends) { $newState=$candidate; break }
                }
            }
            Start-Sleep -Milliseconds 200
        }
        if (-not $newState) { throw 'Automatic watcher startup failed. Inspect the companion logs directory.' }
        Write-Host 'Automatic watcher started. The companion server follows the current-session Osaurus app.'
    } catch {
        if ($newState -and (Test-AssistSupervisorIdentity $newState $root)) { & (Join-Path $PSScriptRoot 'Stop-AutoStart.ps1') -RootPath $root }
        if ($launcherIdentity -and (Test-AssistProcessIdentity $launcherIdentity)) {
            $children = @(Get-AssistOwnedDescendants $launcherIdentity)
            foreach ($child in $children) { if (Test-AssistProcessIdentity $child) { Stop-Process -Id ([int]$child.pid) -ErrorAction SilentlyContinue } }
            if (Test-AssistProcessIdentity $launcherIdentity) { Stop-Process -Id $launcher.Id -ErrorAction SilentlyContinue }
        }
        throw
    }
} finally { Exit-AssistOperationLock $operation }
