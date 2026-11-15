[CmdletBinding()]
param([string]$RootPath, [switch]$Json)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Assert-AssistInstallation (Resolve-AssistRoot $RootPath)
$statePath = Join-Path $root 'state/server.json'
$result = [ordered]@{installed=$true;root=$root;running=$false;healthy=$false;port=$null;pid=$null;identity_matches=$false;idle_policy=$null;
    auto_start=$true;startup_registered=$false;startup_user_disabled=$false;supervisor_running=$false;supervisor_status=$null;osaurus_app_state=$null;automatic_error=$null}
$config = Read-AssistJson (Join-Path $root 'config.json')
if ($config.PSObject.Properties['auto_start']) { $result.auto_start=[bool]$config.auto_start }
try {
    $registration = Assert-AssistAutoRegistration $root
    if ($registration) {
        $result.startup_registered = Test-Path -LiteralPath $registration.shortcut_path -PathType Leaf
        $result.startup_user_disabled = Test-AssistStartupUserDisabled $registration.shortcut_path
    }
} catch { $result.automatic_error='startup_registration_conflict' }
$supervisorPath = Join-Path $root 'state/supervisor.json'
Assert-AssistPathAncestors $supervisorPath
if (Test-Path -LiteralPath $supervisorPath) {
    try {
        $supervisor = Read-AssistJson $supervisorPath
        $result.supervisor_running = Test-AssistSupervisorIdentity $supervisor $root
        if ($result.supervisor_running) {
            $result.supervisor_status=$supervisor.status; $result.osaurus_app_state=$supervisor.app_state
            if ($supervisor.last_error) { $result.automatic_error=$supervisor.last_error }
        } elseif (-not $result.automatic_error) { $result.automatic_error='supervisor_identity_mismatch' }
    } catch { $result.automatic_error='supervisor_tracking_invalid' }
}
if (Test-Path -LiteralPath $statePath) {
    $state = Read-AssistJson $statePath
    $result.port = $state.port; $result.pid = $state.identity.pid
    $result.identity_matches = Test-AssistServerIdentity $state.identity $root
    $result.running = $result.identity_matches
    if ($result.identity_matches) {
        $health = Get-AssistHealth ([int]$state.port)
        $result.healthy = [bool]($health -and $health.service -eq 'osaurus-arm64-assist' -and $health.instance_id -eq $state.instance_id)
        if ($result.healthy -and $health.PSObject.Properties.Name -contains 'idle_policy') { $result.idle_policy = $health.idle_policy }
    }
}
if ($Json) { $result | ConvertTo-Json -Depth 5 } else { [pscustomobject]$result | Format-List }
