[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$RootPath, [string]$ProviderName='Osaurus ARM64 Assist', [string]$OsaurusUrl='http://127.0.0.1:1337', [switch]$RespectStartupDisabled)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Assert-AssistInstallation (Resolve-AssistRoot $RootPath)
$configPath = Join-Path $root 'config.json'; Assert-AssistPathAncestors $configPath
$config = Read-AssistJson $configPath
if (@($config.models).Count -eq 0 -and $config.PSObject.Properties['models_source'] -and $config.models_source -eq 'fixed') { throw 'Fixed model mode requires a registered profile. Automatic mode discovers Osaurus downloads.' }
if (-not $config.api_key -or ([string]$config.api_key).Length -lt 32) { throw 'The local companion API key is missing or too short. Recreate a valid config with the companion init command.' }
$osaurus = Get-AssistOsaurus
$uri = [Uri]$OsaurusUrl
if ($uri.Scheme -ne 'http' -or $uri.Host -notin @('127.0.0.1','localhost','[::1]','::1') -or $uri.Port -lt 1 -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne '/') { throw 'OsaurusUrl must be a loopback HTTP origin without credentials or a path.' }
if (-not $PSCmdlet.ShouldProcess($ProviderName, 'Register this companion as an Osaurus OpenAI-compatible provider')) { return }
if ($PSBoundParameters.ContainsKey('OsaurusUrl')) {
    # A deliberately selected Osaurus endpoint is also the preference source.
    # Default connections keep automatic port discovery, requiring no setting.
    $config | Add-Member -NotePropertyName osaurus_url -NotePropertyValue $uri.GetLeftPart([UriPartial]::Authority) -Force
    Write-AssistJson $configPath $config
}
$null = & $osaurus serve
if ($LASTEXITCODE -ne 0) { throw 'Osaurus did not start. Launch the app and retry.' }
$startAutomatically = -not $config.PSObject.Properties['auto_start'] -or $config.auto_start -eq $true
if ($RespectStartupDisabled -and $startAutomatically) {
    $descriptor = Get-AssistAutoStartDescriptor $root
    if (Test-AssistStartupUserDisabled $descriptor.shortcut_path) { $startAutomatically = $false }
}
if ($startAutomatically) {
    & (Join-Path $PSScriptRoot 'Enable-AutoStart.ps1') -RootPath $root
    & (Join-Path $PSScriptRoot 'Start.ps1') -RootPath $root -AutomaticOwnerId (Get-AssistInstallationId $root)
} else { & (Join-Path $PSScriptRoot 'Start.ps1') -RootPath $root }
$statePath = Join-Path $root 'state/osaurus-registration.json'; Assert-AssistPathAncestors $statePath
$marker = Read-AssistJson (Join-Path $root '.osaurus-arm64-assist-install.json')
$server = Read-AssistJson (Join-Path $root 'state/server.json')
$baseUrl = "http://127.0.0.1:$($server.port)/v1"
$keyRef = 'keychain:osaurus-arm64-assist/' + $marker.installation_id
$export = Read-AssistOsaurusExport $osaurus
$matches = @($export.providers | Where-Object {$_.name -ieq $ProviderName})
if ($matches.Count -gt 1) { throw 'Multiple providers have this name; no configuration was changed.' }
$registration = $null
if (Test-Path -LiteralPath $statePath) {
    $registration = Read-AssistJson $statePath
    if ($registration.installation_id -ne $marker.installation_id -or $registration.provider_name -ine $ProviderName) { throw 'Registration belongs to another installation or provider name.' }
}
if ($matches.Count -eq 1) {
    if (-not $registration) { throw 'An existing provider has this name but is not owned by this installation. Choose another -ProviderName.' }
    Assert-AssistOwnedProvider $matches[0] $registration
    if ($registration.status -eq 'connected') {
        if ($registration.base_url.TrimEnd('/') -cne $baseUrl.TrimEnd('/')) { throw 'The companion port changed. Disconnect the old registration before connecting its new endpoint.' }
        Write-Host "'$ProviderName' is already registered. Existing provider credential references were not rewritten."
        return
    }
}
$recipePath = Join-Path $root 'state/osaurus-provider-plan.json'; Assert-AssistPathAncestors $recipePath
Write-AssistJson $recipePath @{version=1;providers=@(@{name=$ProviderName;provider='custom';enabled=$true;base_url=$baseUrl;set_api_key=$false;api_key_ref=$keyRef})}
$null = & $osaurus config plan $recipePath
if ($LASTEXITCODE -ne 0) { throw 'Osaurus rejected the provider recipe; nothing was applied.' }
$registration = [ordered]@{version=1;installation_id=$marker.installation_id;provider_name=$ProviderName;provider_type='openaicompatible';base_url=$baseUrl;enabled=$true;key_ref=$keyRef;credential_created=$false;status='pending';provider_id=$null;model_ids=@($config.models | ForEach-Object {$_.id});created_utc=[DateTime]::UtcNow.ToString('o')}
Write-AssistJson $statePath $registration
$config.api_key | & $osaurus import secret set $keyRef
if ($LASTEXITCODE -ne 0) { throw 'Osaurus could not store the companion credential.' }
$registration['credential_created']=$true; Write-AssistJson $statePath $registration
$null = & $osaurus config apply $recipePath --yes
if ($LASTEXITCODE -ne 0) { throw 'Provider apply failed; the owned pending registration is retained for safe cleanup.' }
$after = Read-AssistOsaurusExport $osaurus
$provider = @($after.providers | Where-Object {$_.name -ieq $ProviderName})
if ($provider.Count -ne 1) { throw 'Provider was not present after apply; inspect Osaurus before retrying.' }
Assert-AssistOwnedProvider $provider[0] ([pscustomobject]$registration)
try {
    # Automatic profiles are intentionally absent from config.json. Discover
    # their IDs from the authenticated companion endpoint when available.
    $companionModels = Invoke-AssistLocalJson -Url ($baseUrl + '/models') -ApiKey ([string]$config.api_key) -TimeoutSec 10
    $registration['model_ids'] = @($companionModels.data | ForEach-Object {$_.id})
    $models = Invoke-AssistLocalJson -Url ($OsaurusUrl.TrimEnd('/') + '/v1/models') -TimeoutSec 10
    $owners = @($models.data | Where-Object {$_.id -in $registration.model_ids -and $_.owned_by -like 'prov-*'} | Select-Object -ExpandProperty owned_by -Unique)
    if ($owners.Count -eq 1) { $registration['provider_id']=$owners[0] }
} catch { Write-Verbose 'Provider ID discovery is unavailable; the provider registration itself is verified.' }
$registration['status']='connected'; Write-AssistJson $statePath $registration
Write-Host "Connected '$ProviderName'. Select its model explicitly in Osaurus; existing agents and defaults were not changed."
