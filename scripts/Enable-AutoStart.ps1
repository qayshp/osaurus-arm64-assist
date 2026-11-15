[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$RootPath,[switch]$RegistrationOnly)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Assert-AssistInstallation (Resolve-AssistRoot $RootPath)
$operation = Enter-AssistOperationLock $root 'Automatic'
$serverOperation = $null
try {
    $null = Assert-AssistAutoRegistration $root
    $transfer = $false
    if (-not $RegistrationOnly) {
        $serverOperation = Enter-AssistOperationLock $root 'Server'
        $transfer = Get-AssistManualServerTransferRequired $root
    }
    if (-not $PSCmdlet.ShouldProcess($root,'Enable the owned current-user Startup shortcut and automatic Osaurus lifecycle')) { return }
    $registration = Register-AssistAutoStart $root
    Set-AssistAutoStartConfig $root $true
    if (-not $RegistrationOnly) {
        # An explicit first-time enable transfers an existing owned manual
        # server to automatic lifecycle. Repeated enables keep an owned
        # automatic server warm.
        if ($transfer) { & (Join-Path $PSScriptRoot 'Stop.ps1') -RootPath $root -ServerOnly }
        Exit-AssistOperationLock $serverOperation; $serverOperation=$null
        & (Join-Path $PSScriptRoot 'Start-AutoStart.ps1') -RootPath $root
    }
    Write-Host 'Registered automatic lifecycle in this user''s Startup folder.'
    if (Test-AssistStartupUserDisabled $registration.shortcut_path) { Write-Host 'Windows Startup Apps currently disables this shortcut; that preference was preserved.' }
} finally {
    if ($serverOperation) { Exit-AssistOperationLock $serverOperation }
    Exit-AssistOperationLock $operation
}
