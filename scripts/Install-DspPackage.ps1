[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$RootPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Assert-AssistInstallation (Resolve-AssistRoot $RootPath)
$lock = Read-AssistJson (Join-Path $root 'app/runtime.lock.json')
$runtime = Join-Path $root ('runtime/' + $lock.archive.root)
Assert-AssistNoReparsePoints $runtime
Assert-AssistRuntime $runtime $lock
$catalog = Join-Path $runtime 'lib/llama_cpp/libggml-htp.cat'
if ((Get-AuthenticodeSignature -LiteralPath $catalog).Status -ne 'Valid') { throw 'The pinned DSP catalog does not have a valid Windows signature; provisioning stopped.' }
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'This optional signed-DSP provisioning step requires an elevated PowerShell terminal. The ordinary companion installer does not require elevation.'
}
$inf = Join-Path $runtime 'lib/llama_cpp/libggml-htp.inf'
if ($PSCmdlet.ShouldProcess($inf, 'Register the pinned production-signed GGML DSP library package in Windows Driver Store')) {
    & pnputil.exe /add-driver $inf /install
    if ($LASTEXITCODE -notin @(0,3010)) { throw "Signed DSP package provisioning failed with exit code $LASTEXITCODE" }
    Write-Host 'Signed DSP library package registered. Hardware-driver updates remain managed by Windows Update or the device vendor.'
}
