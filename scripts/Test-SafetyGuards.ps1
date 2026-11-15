[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$base = Join-Path ([IO.Path]::GetTempPath()) ('OsaurusAssistSafety-' + [guid]::NewGuid().ToString())
$root = Join-Path $base 'install'
$outside = Join-Path $base 'outside'
$helper = $null; $helperIdentity = $null; $junctions = @(); $passed = New-Object 'Collections.Generic.List[string]'
function Assert-Test {
    param([bool]$Condition,[string]$Name)
    if (-not $Condition) { throw "Safety assertion failed: $Name" }
    $passed.Add($Name)
}
function Assert-Refusal {
    param([scriptblock]$Action,[string]$Name)
    $refused=$false
    try { & $Action | Out-Null } catch { $refused=$true }
    Assert-Test $refused $Name
}
try {
    New-Item -ItemType Directory -Force -Path $root,$outside,(Join-Path $root 'state') | Out-Null
    $sentinel = Join-Path $outside 'preserve.txt'
    [IO.File]::WriteAllText($sentinel,'outside sentinel')
    Write-AssistJson (Join-Path $root '.osaurus-arm64-assist-install.json') @{product=$script:AssistProduct;root=$root;installation_id=[guid]::NewGuid().ToString()}
    Write-AssistJson (Join-Path $root 'config.json') @{schema_version=1;models=@();port=65000;api_key=('x'*48)}
    Assert-Refusal { Resolve-AssistRoot ([IO.Path]::GetPathRoot($root)) } 'drive root refused'
    Assert-Refusal { Resolve-AssistRoot $env:USERPROFILE } 'profile root refused'
    $alias = Join-Path $base 'alias'
    New-Item -ItemType Junction -Path $alias -Target $outside | Out-Null; $junctions += $alias
    Assert-Refusal { Resolve-AssistRoot $alias } 'root junction refused'
    Assert-Refusal { Resolve-AssistRoot (Join-Path $alias 'nested-install') } 'ancestor junction refused'
    [IO.Directory]::Delete($alias); $junctions = @($junctions | Where-Object {$_ -ne $alias})
    $nested = Join-Path $root 'nested-link'
    New-Item -ItemType Junction -Path $nested -Target $outside | Out-Null; $junctions += $nested
    Assert-Refusal { Assert-AssistNoReparsePoints $root } 'nested junction refused'
    [IO.Directory]::Delete($nested); $junctions = @($junctions | Where-Object {$_ -ne $nested})
    $weights = Join-Path $root 'user-model.gguf'
    [IO.File]::WriteAllText($weights,'synthetic model-preservation fixture')
    Write-AssistJson (Join-Path $root 'config.json') @{schema_version=1;models=@(@{id='fixture';path=$weights;mode='cpu'});port=65000;api_key=('x'*48)}
    Assert-Refusal { & (Join-Path $PSScriptRoot 'Uninstall.ps1') -RootPath $root } 'configured model inside install refused'
    Assert-Test (Test-Path -LiteralPath $weights) 'configured model preserved'
    Write-AssistJson (Join-Path $root 'config.json') @{schema_version=1;models=@();port=65000;api_key=('x'*48)}
    Assert-Refusal { Assert-AssistModelsOutsideInstallation $root } 'unregistered GGUF inside install refused'
    Remove-Item -LiteralPath $weights -Force
    $idle = Join-Path $base 'idle.ps1'
    [IO.File]::WriteAllText($idle,'Start-Sleep -Seconds 45')
    $powershell = (Get-Process -Id $PID).Path
    $helper = Start-Process -FilePath $powershell -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File ' + (Quote-AssistArgument $idle)) -WindowStyle Hidden -PassThru
    $helperIdentity = Get-AssistProcessIdentity $helper.Id
    Assert-Test ([bool]$helperIdentity) 'test helper identity captured'
    Assert-Test (Test-AssistProcessIdentity $helperIdentity) 'matching process identity accepted'
    $bad = [pscustomobject]@{pid=$helperIdentity.pid;start_time_utc='2000-01-01T00:00:00.0000000Z';executable=$helperIdentity.executable;command_line_sha256=$helperIdentity.command_line_sha256}
    Assert-Test (-not (Test-AssistProcessIdentity $bad)) 'reused or mismatched PID rejected'
    Write-AssistJson (Join-Path $root 'state/server.json') @{identity=$bad;port=65000;instance_id='fixture';launcher_identity=$null}
    & (Join-Path $PSScriptRoot 'Stop.ps1') -RootPath $root
    Assert-Test (-not $helper.HasExited) 'stale PID cleanup preserves reused process'
    Assert-Test (-not (Test-Path -LiteralPath (Join-Path $root 'state/server.json'))) 'stale server tracking removed'
    Assert-Test (-not (Test-AssistServerIdentity $helperIdentity $root)) 'unrelated process rejected as server'
    Write-AssistJson (Join-Path $root 'state/server.json') @{identity=$helperIdentity;port=65000;instance_id='fixture';launcher_identity=$null}
    Assert-Refusal { & (Join-Path $PSScriptRoot 'Stop.ps1') -RootPath $root } 'stop preserves unrelated process'
    Assert-Test (-not $helper.HasExited) 'unrelated process remains alive'
    Assert-Refusal { & (Join-Path $PSScriptRoot 'Uninstall.ps1') -RootPath $root } 'uninstall refuses unrelated recorded process'
    Assert-Test (Test-Path -LiteralPath (Join-Path $root '.osaurus-arm64-assist-install.json')) 'failed uninstall retains installation'
    & (Join-Path $PSScriptRoot 'Uninstall.ps1') -RootPath $root -WhatIf
    Assert-Test (Test-Path -LiteralPath $root) 'what-if uninstall preserves installation'
    Remove-Item -LiteralPath (Join-Path $root 'state/server.json') -Force
    $externalWeights = Join-Path $outside 'external-model.gguf'
    [IO.File]::WriteAllText($externalWeights,'external model fixture')
    Write-AssistJson (Join-Path $root 'config.json') @{schema_version=1;models=@(@{id='outside';path=$externalWeights;mode='cpu'});port=65000;api_key=('x'*48)}
    & (Join-Path $PSScriptRoot 'Uninstall.ps1') -RootPath $root
    Assert-Test (-not (Test-Path -LiteralPath $root)) 'owned installation uninstalled successfully'
    Assert-Test ((Get-Content -LiteralPath $externalWeights -Raw) -eq 'external model fixture') 'external model survives uninstall'
    Assert-Test (-not $helper.HasExited) 'external process survives successful uninstall'
    Assert-Test ((Get-Content -LiteralPath $sentinel -Raw) -eq 'outside sentinel') 'outside sentinel preserved'
    [pscustomobject]@{passed=$passed.Count;checks=@($passed)} | ConvertTo-Json -Depth 4
} finally {
    if ($helperIdentity -and (Test-AssistProcessIdentity $helperIdentity)) { Stop-Process -Id $helperIdentity.pid -ErrorAction SilentlyContinue }
    foreach ($junction in $junctions) { if (Test-Path -LiteralPath $junction) { [IO.Directory]::Delete($junction) } }
    if (Test-Path -LiteralPath $base) {
        $checked = [IO.Path]::GetFullPath($base)
        $prefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\OsaurusAssistSafety-'
        if (-not $checked.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { throw 'Fixture cleanup target escaped its temporary sandbox.' }
        Assert-AssistNoReparsePoints $checked
        Remove-Item -LiteralPath $checked -Recurse -Force
    }
}
