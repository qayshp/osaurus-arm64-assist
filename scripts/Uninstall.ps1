[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$RootPath, [switch]$KeepConfig, [string]$BackupPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Assert-AssistInstallation (Resolve-AssistRoot $RootPath)
Assert-AssistNoReparsePoints $root
Assert-AssistModelsOutsideInstallation $root
$null = Assert-AssistAutoRegistration $root
$backup = $null
if ($KeepConfig) {
    if (-not $BackupPath) { $BackupPath = $root + '.config-backup.json' }
    $backup = [IO.Path]::GetFullPath($BackupPath)
    if ($backup.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase) -or $backup.Equals($root, [StringComparison]::OrdinalIgnoreCase)) { throw 'The backup must be outside the installation being removed.' }
    Assert-AssistPathAncestors $backup
    if (Test-Path -LiteralPath $backup) { throw 'The backup file already exists; choose another path to preserve it.' }
}
if (-not $PSCmdlet.ShouldProcess($root, 'Remove owned Startup registration, disconnect provider, stop owned processes, and remove the owned companion installation')) { return }
$automaticOperation = Enter-AssistOperationLock $root 'Automatic'
try {
if ($KeepConfig) {
    Invoke-AssistPython (Join-Path $root '.venv/Scripts/python.exe') @('-m','osaurus_arm64_assist','config','export','--config',(Join-Path $root 'config.json'),'--output',$backup)
}
& (Join-Path $PSScriptRoot 'Disable-AutoStart.ps1') -RootPath $root
$registration = Join-Path $root 'state/osaurus-registration.json'
if (Test-Path -LiteralPath $registration) { & (Join-Path $PSScriptRoot 'Disconnect-Osaurus.ps1') -RootPath $root }
& (Join-Path $PSScriptRoot 'Stop.ps1') -RootPath $root -ServerOnly
$checkedRoot = Assert-AssistInstallation $root
if (-not $checkedRoot.Equals($root,[StringComparison]::OrdinalIgnoreCase)) { throw 'Installation target changed during uninstall.' }
Assert-AssistNoReparsePoints $checkedRoot
Remove-Item -LiteralPath $checkedRoot -Recurse -Force
Write-Host 'Companion removed. Model files, driver packages, other apps, and Windows settings were preserved.'
if ($backup) { Write-Host "Tokenless configuration backup: $backup" }
} finally { Exit-AssistOperationLock $automaticOperation }
