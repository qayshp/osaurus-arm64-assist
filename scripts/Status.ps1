[CmdletBinding()]
param([string]$RootPath, [switch]$Json)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Assert-AssistInstallation (Resolve-AssistRoot $RootPath)
$statePath = Join-Path $root 'state/server.json'
$result = [ordered]@{installed=$true;root=$root;running=$false;healthy=$false;port=$null;pid=$null;identity_matches=$false;idle_policy=$null}
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
