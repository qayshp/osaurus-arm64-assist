[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$RootPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Assert-AssistInstallation (Resolve-AssistRoot $RootPath)
$operation = Enter-AssistOperationLock $root 'Automatic'
try {
    $null = Assert-AssistAutoRegistration $root
    if (-not $PSCmdlet.ShouldProcess($root,'Remove the owned Startup shortcut and stop automatic watcher and managed server')) { return }
    Set-AssistAutoStartConfig $root $false
    Unregister-AssistAutoStart $root
    & (Join-Path $PSScriptRoot 'Stop-AutoStart.ps1') -RootPath $root
    Write-Host 'Automatic lifecycle disabled. Any independently started manual server was preserved.'
} finally { Exit-AssistOperationLock $operation }
