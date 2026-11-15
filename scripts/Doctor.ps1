[CmdletBinding()]
param([string]$RootPath, [string]$ModelId, [switch]$Smoke, [switch]$Json)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Assert-AssistInstallation (Resolve-AssistRoot $RootPath)
$lock = Read-AssistJson (Join-Path $root 'app/runtime.lock.json')
$runtime = Join-Path $root ('runtime/' + $lock.archive.root)
$integrity = $true
try { Assert-AssistRuntime $runtime $lock } catch { $integrity = $false }
$catalog = Join-Path $runtime 'lib/llama_cpp/libggml-htp.cat'
$signature = Get-AuthenticodeSignature -LiteralPath $catalog -ErrorAction SilentlyContinue
$signatureStatus = 'Missing'
if ($signature) { $signatureStatus = [string]$signature.Status }
$drivers = @(Get-CimInstance Win32_PnPSignedDriver | Where-Object {$_.DeviceName -match 'Hexagon|Adreno|Neural Processing|NPU'} | Select-Object DeviceName,DriverVersion,DriverProviderName,IsSigned)
$packageInventory = (& pnputil.exe /enum-drivers /class ComputeAccelerator 2>$null | Out-String)
$registeredHint = $packageInventory -match 'libggml-htp\.inf'
$native = & (Join-Path $root '.venv/Scripts/python.exe') -m osaurus_arm64_assist doctor --config (Join-Path $root 'config.json') --json
$nativeExit = $LASTEXITCODE
try { $nativeObject = $native | ConvertFrom-Json } catch { $nativeObject = @{error='Companion doctor did not return valid JSON.'} }
$result = [ordered]@{
    runtime_integrity=$integrity;catalog_signature_status=$signatureStatus
    signed_dsp_package_registered_hint=[bool]$registeredHint
    signed_dsp_registration_note='An inventory hint or a downloaded signed catalog does not prove this machine can run the DSP. Actual NPU readiness requires a model smoke test. Registered packages are not modified by doctor.'
    hardware_drivers=$drivers;companion=$nativeObject;companion_exit_code=$nativeExit
    model_smoke_requested=$false
}
if ($Smoke) {
    if (-not $ModelId) { throw 'Use -ModelId with -Smoke to select an explicit configured model.' }
    $smokeResult = & (Join-Path $root '.venv/Scripts/python.exe') -m osaurus_arm64_assist self-test --config (Join-Path $root 'config.json') --model $ModelId
    $result['model_smoke_requested'] = $true
    $result['smoke_exit_code'] = $LASTEXITCODE
    try { $result.smoke = $smokeResult | ConvertFrom-Json } catch { $result.smoke = @{output='See companion self-test command output.'} }
}
if ($Json) { $result | ConvertTo-Json -Depth 12 } else { [pscustomobject]$result | Format-List }
if (-not $integrity -or $nativeExit -ne 0 -or ($Smoke -and $result.smoke_exit_code -ne 0)) { exit 1 }
