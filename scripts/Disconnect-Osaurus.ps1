[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$RootPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Assert-AssistInstallation (Resolve-AssistRoot $RootPath)
$statePath = Join-Path $root 'state/osaurus-registration.json'; Assert-AssistPathAncestors $statePath
if (-not (Test-Path -LiteralPath $statePath)) { Write-Host 'No owned Osaurus registration.'; return }
$registration = Read-AssistJson $statePath
$marker = Read-AssistJson (Join-Path $root '.osaurus-arm64-assist-install.json')
if ($registration.installation_id -ne $marker.installation_id) { throw 'Registration ownership does not match the installation.' }
if ($registration.key_ref -cne ('keychain:osaurus-arm64-assist/'+$marker.installation_id)) { throw 'Credential reference is not owned by this installation.' }
$osaurus = Get-AssistOsaurus
if (-not $PSCmdlet.ShouldProcess($registration.provider_name, 'Remove only the owned Osaurus provider and its unique credential')) { return }
$providerOperation = Enter-AssistOperationLock $root 'Provider'
try {
$registration = Read-AssistJson $statePath
if ($registration.installation_id -ne $marker.installation_id -or $registration.key_ref -cne ('keychain:osaurus-arm64-assist/'+$marker.installation_id)) {
    throw 'Provider registration changed while waiting for the ownership lock; nothing was removed.'
}
$null = & $osaurus serve
if ($LASTEXITCODE -ne 0) { throw 'Osaurus did not start. Launch it before disconnecting so its other settings can be preserved.' }
if ($registration.PSObject.Properties['provider_name_pending']) {
    $origin = 'http://127.0.0.1:1337'
    $config = Read-AssistJson (Join-Path $root 'config.json')
    if ($config.PSObject.Properties['osaurus_url'] -and $config.osaurus_url -and $config.osaurus_url -ne 'auto') { $origin = [string]$config.osaurus_url }
    # Resolve an interrupted rename by its same provider ID before cleanup.
    # ResolveOnly never issues PUT or guesses which label currently exists.
    $registration = Set-AssistOwnedProviderName -RootPath $root -Osaurus $osaurus -OsaurusUrl $origin -Registration $registration -Name $registration.provider_name -ResolveOnly
}
$export = Read-AssistOsaurusExport $osaurus
$matching = @($export.providers | Where-Object {$_.name -ieq $registration.provider_name})
if ($matching.Count -gt 1) { throw 'Provider name is ambiguous; nothing was changed.' }
if ($matching.Count -eq 1) {
    Assert-AssistOwnedProvider $matching[0] $registration
    $references = @($export.agents)
    if ($export.default_agent) { $references += $export.default_agent }
    foreach ($agent in $references) {
        if ($agent.PSObject.Properties['model'] -and $agent.model) {
            $model = [string]$agent.model
            if ($model -in @($registration.model_ids) -or ($registration.provider_id -and $model.StartsWith($registration.provider_id+'/',[StringComparison]::OrdinalIgnoreCase))) {
                throw 'An agent or default agent currently selects this provider. Choose another model in Osaurus before disconnecting; agent settings were preserved.'
            }
        }
    }
    # Prune is scoped to providers because no other section occurs in this document.
    $keepers = @($export.providers | Where-Object {$_.name -ine $registration.provider_name} | ForEach-Object {@{name=$_.name}})
    $recipePath = Join-Path $root 'state/osaurus-disconnect-plan.json'; Assert-AssistPathAncestors $recipePath
    Write-AssistJson $recipePath @{version=1;providers=$keepers}
    $null = & $osaurus config plan $recipePath --prune
    if ($LASTEXITCODE -ne 0) { throw 'Osaurus rejected the scoped removal plan; nothing was applied.' }
    $latest = Read-AssistOsaurusExport $osaurus
    $originalProviders = @($export.providers | Sort-Object name) | ConvertTo-Json -Depth 30 -Compress
    $latestProviders = @($latest.providers | Sort-Object name) | ConvertTo-Json -Depth 30 -Compress
    if ($originalProviders -cne $latestProviders) { throw 'Provider state changed while planning. Nothing was applied; retry without concurrent provider edits.' }
    $null = & $osaurus config apply $recipePath --prune --yes
    if ($LASTEXITCODE -ne 0) { throw 'Scoped provider removal failed; registration was retained.' }
    $after = Read-AssistOsaurusExport $osaurus
    if (@($after.providers | Where-Object {$_.name -ieq $registration.provider_name}).Count -ne 0) { throw 'Owned provider was still present after removal.' }
    $beforeOther = @($export.providers | Where-Object {$_.name -ine $registration.provider_name} | Sort-Object name) | ConvertTo-Json -Depth 30 -Compress
    $afterOther = @($after.providers | Sort-Object name) | ConvertTo-Json -Depth 30 -Compress
    if ($beforeOther -cne $afterOther) { throw 'Other provider state changed during removal; inspect Osaurus. Ownership state was retained.' }
}
if ($registration.credential_created) {
    $null = & $osaurus import secret delete $registration.key_ref
    if ($LASTEXITCODE -ne 0) { throw 'Provider removed but credential cleanup failed; registration was retained for retry.' }
}
Remove-Item -LiteralPath $statePath -Force
Write-Host 'Owned Osaurus provider and credential disconnected. Other providers, agents and defaults were preserved.'
} finally { Exit-AssistOperationLock $providerOperation }
