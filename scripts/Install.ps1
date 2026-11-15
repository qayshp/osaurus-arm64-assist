[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$RootPath, [string]$PythonPath, [string]$RuntimeArchive, [string]$ConfigBackup, [switch]$ConnectOsaurus, [switch]$NoAutoStart)
$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Resolve-AssistRoot $RootPath
if (-not [Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([Runtime.InteropServices.OSPlatform]::Windows) -or
    [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne 'Arm64') { throw 'This package requires Windows on ARM64.' }
if ($root.Equals([IO.Path]::GetFullPath($sourceRoot).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) { throw 'Install into a dedicated directory, not the source checkout.' }
if ([IO.Path]::GetFullPath($sourceRoot).StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Run Install.ps1 from a source checkout outside the installation directory.' }
$python = Get-AssistPython $PythonPath
$lock = Read-AssistJson (Join-Path $sourceRoot 'runtime.lock.json')
if (Test-Path -LiteralPath $root) {
    if (Test-Path -LiteralPath (Join-Path $root '.osaurus-arm64-assist-install.json')) {
        $null = Assert-AssistInstallation $root
        Assert-AssistModelsOutsideInstallation $root
        $null = Assert-AssistAutoRegistration $root
    }
    elseif (@(Get-ChildItem -LiteralPath $root -Force).Count -gt 0) { throw 'The target is nonempty and is not an owned installation.' }
}
if (-not $PSCmdlet.ShouldProcess($root, 'Install companion, pinned runtime and private Python environment')) { return }
New-Item -ItemType Directory -Force -Path $root | Out-Null
$markerPath = Join-Path $root '.osaurus-arm64-assist-install.json'
if (-not (Test-Path -LiteralPath $markerPath)) {
    Write-AssistJson $markerPath @{product=$script:AssistProduct;root=$root;installation_id=[guid]::NewGuid().ToString();created_utc=[DateTime]::UtcNow.ToString('o')}
}
$automaticOperation = Enter-AssistOperationLock $root 'Automatic'
try {
& (Join-Path $PSScriptRoot 'Stop-AutoStart.ps1') -RootPath $root
$serverOperation = Enter-AssistOperationLock $root 'Server'
try {
$statePath = Join-Path $root 'state/server.json'
if (Test-Path -LiteralPath $statePath) {
    & (Join-Path $PSScriptRoot 'Stop.ps1') -RootPath $root -ServerOnly
}
foreach ($folder in @('app','cache','runtime','state','logs')) {
    $folderPath = Join-Path $root $folder
    Assert-AssistPathAncestors $folderPath
    New-Item -ItemType Directory -Force -Path $folderPath | Out-Null
}
$archive = Join-Path $root ('cache/' + $lock.archive.filename)
Assert-AssistPathAncestors $archive
if ($RuntimeArchive) {
    $provided = [IO.Path]::GetFullPath($RuntimeArchive)
    if (-not (Test-Path -LiteralPath $provided -PathType Leaf)) { throw 'The supplied runtime archive does not exist.' }
    if (-not $provided.Equals($archive, [StringComparison]::OrdinalIgnoreCase)) { Copy-Item -LiteralPath $provided -Destination $archive }
}
if (-not (Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $lock.archive.sha256) {
    if ($RuntimeArchive) { throw 'The supplied runtime archive does not match runtime.lock.json.' }
    Write-Host 'Downloading the pinned official Qualcomm runtime...'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $lock.archive.url -OutFile $archive -UseBasicParsing
}
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $lock.archive.sha256) { throw 'Downloaded runtime checksum mismatch; installation stopped.' }
$runtimeBase = Join-Path $root 'runtime'
Assert-AssistNoReparsePoints $runtimeBase
$runtime = Join-Path $runtimeBase $lock.archive.root
$runtimeOkay = $false
if (Test-Path -LiteralPath $runtime) { try { Assert-AssistRuntime $runtime $lock; $runtimeOkay = $true } catch { } }
if (-not $runtimeOkay) {
    if (Test-Path -LiteralPath $runtime) {
        if (-not ([IO.Path]::GetFullPath($runtime)).StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Runtime path escaped the installation.' }
        Assert-AssistNoReparsePoints $runtime
        Remove-Item -LiteralPath $runtime -Recurse -Force
    }
    Assert-AssistZipPaths $archive $runtimeBase
    Expand-Archive -LiteralPath $archive -DestinationPath $runtimeBase -Force
    Assert-AssistRuntime $runtime $lock
}
$app = [IO.Path]::GetFullPath((Join-Path $root 'app'))
if (-not $app.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Application path escaped the owned installation.' }
Assert-AssistNoReparsePoints $app
Remove-Item -LiteralPath $app -Recurse -Force
New-Item -ItemType Directory -Path $app | Out-Null
foreach ($name in @('pyproject.toml','requirements.lock','README.md','LICENSE','THIRD_PARTY_NOTICES.md','CONTRIBUTING.md','runtime.lock.json','src','scripts','docs','evidence')) {
    $item = Join-Path $sourceRoot $name
    if (Test-Path -LiteralPath $item) { Copy-Item -LiteralPath $item -Destination (Join-Path $root 'app') -Recurse -Force }
}
$venv = Join-Path $root '.venv'
Assert-AssistPathAncestors $venv
if (-not (Test-Path -LiteralPath (Join-Path $venv 'Scripts/python.exe'))) { Invoke-AssistPython $python @('-I','-m','venv',$venv) }
$environmentPython = Join-Path $venv 'Scripts/python.exe'
$requirements = Join-Path $root 'app/requirements.lock'
if (Test-Path -LiteralPath $requirements) { Invoke-AssistPython $environmentPython @('-I','-m','pip','install','--disable-pip-version-check','--require-hashes','--only-binary=:all:','-r',$requirements) }
Invoke-AssistPython $environmentPython @('-I','-m','pip','install','--disable-pip-version-check','--no-build-isolation','--no-deps','--force-reinstall',(Join-Path $root 'app'))
$config = Join-Path $root 'config.json'
Assert-AssistPathAncestors $config
if (-not (Test-Path -LiteralPath $config)) {
    Invoke-AssistPython $environmentPython @('-m','osaurus_arm64_assist','init','--config',$config,'--runtime-dir',(Join-Path $runtime 'lib/llama_cpp'))
}
if ($ConfigBackup) {
    $backup = [IO.Path]::GetFullPath($ConfigBackup)
    if (-not (Test-Path -LiteralPath $backup -PathType Leaf)) { throw 'The configuration backup does not exist.' }
    Invoke-AssistPython $environmentPython @('-m','osaurus_arm64_assist','config','import','--config',$config,'--input',$backup)
}
if ($NoAutoStart) { Set-AssistAutoStartConfig $root $false }
} finally { Exit-AssistOperationLock $serverOperation }
$installedConfig = Read-AssistJson (Join-Path $root 'config.json')
$autoEnabled = -not $installedConfig.PSObject.Properties['auto_start'] -or $installedConfig.auto_start -eq $true
if ($autoEnabled) {
    $descriptor = Get-AssistAutoStartDescriptor $root
    if (Test-AssistStartupUserDisabled $descriptor.shortcut_path) {
        & (Join-Path $PSScriptRoot 'Enable-AutoStart.ps1') -RootPath $root -RegistrationOnly
    } else {
        & (Join-Path $PSScriptRoot 'Enable-AutoStart.ps1') -RootPath $root
    }
} else { & (Join-Path $PSScriptRoot 'Disable-AutoStart.ps1') -RootPath $root }
if ($ConnectOsaurus) { & (Join-Path $PSScriptRoot 'Connect-Osaurus.ps1') -RootPath $root -RespectStartupDisabled }
Write-Host "Installed at $root"
Write-Host 'Model weights and drivers were preserved. Automatic mode uses only this user''s owned Startup shortcut.'
Write-Host 'Models and supported preferences are discovered from Osaurus automatically. Use -NoAutoStart for manual lifecycle.'
} finally { Exit-AssistOperationLock $automaticOperation }
