[CmdletBinding()]
param([string]$PythonPath)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
if (-not $PythonPath) { $PythonPath=(Get-Command python.exe -ErrorAction Stop).Source }
$base=Join-Path ([IO.Path]::GetTempPath()) ('Osaurus Assist Http-'+[guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $base | Out-Null
$ready=Join-Path $base 'ready.json'
$fixture=$null; $identity=$null
$previousProxy=[Net.WebRequest]::DefaultWebProxy
$previousHttp=$env:HTTP_PROXY; $previousHttps=$env:HTTPS_PROXY; $previousNoProxy=$env:NO_PROXY
try {
    $arguments=@((Join-Path $PSScriptRoot 'test_loopback_http.py'),'--ready',$ready)
    $fixture=Start-Process -FilePath $PythonPath -ArgumentList (@($arguments | ForEach-Object {Quote-AssistArgument $_}) -join ' ') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $base 'fixture.stdout.log') -RedirectStandardError (Join-Path $base 'fixture.stderr.log')
    $identity=Get-AssistProcessIdentity $fixture.Id
    $clock=[Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Path -LiteralPath $ready) -and $clock.Elapsed.TotalSeconds -lt 10) {Start-Sleep -Milliseconds 50}
    if (-not (Test-Path -LiteralPath $ready)) {throw 'HTTP test fixture did not start.'}
    $ports=Read-AssistJson $ready
    $target="http://127.0.0.1:$($ports.target_port)"
    $proxyUrl="http://127.0.0.1:$($ports.proxy_port)"
    $proxy=New-Object Net.WebProxy($proxyUrl,$false)
    [Net.WebRequest]::DefaultWebProxy=$proxy
    $env:HTTP_PROXY=$proxyUrl; $env:HTTPS_PROXY=$proxyUrl; $env:NO_PROXY=''
    # A reserved non-loopback name makes the explicit proxy control work on
    # .NET Framework, which can special-case loopback before consulting a proxy.
    $negative=[Net.HttpWebRequest]::Create('http://osaurus-assist-negative-control.invalid/check')
    $negative.Proxy=$proxy; $negative.Timeout=2000
    $negativeResponse=$negative.GetResponse()
    $reader=New-Object IO.StreamReader($negativeResponse.GetResponseStream())
    try {$negativeData=$reader.ReadToEnd() | ConvertFrom-Json} finally {$reader.Dispose();$negativeResponse.Dispose()}
    if (-not $negativeData.proxy) {throw 'Negative control did not exercise the proxy.'}
    $before=(Invoke-AssistLocalJson -Url ($target+'/stats')).proxy_hits
    if ($before -ne 1) {throw 'Proxy capture did not register the negative control.'}
    $health=Invoke-AssistLocalJson -Url ($target+'/health')
    if (-not $health.direct) {throw 'Loopback GET was redirected to the proxy.'}
    $shutdown=Invoke-AssistLocalJson -Url ($target+'/admin/shutdown') -Method POST -ApiKey 'loopback-test-key'
    if (-not $shutdown.direct -or -not $shutdown.authorized) {throw 'Authenticated loopback POST did not reach the target directly.'}
    $label='NPU Hybrid '+[char]0x00E9
    $renamed=Invoke-AssistLocalJson -Url ($target+'/admin/providers/fixture') -Method PUT -Body @{name=$label} -ApiKey 'loopback-test-key'
    if (-not $renamed.direct -or -not $renamed.authorized -or $renamed.body.name -cne $label -or
        @($renamed.body.PSObject.Properties).Count -ne 1 -or $renamed.content_type -notlike 'application/json*') {throw 'Name-only UTF-8 PUT was not preserved.'}
    $refused=$false
    try {$null=Invoke-AssistLocalJson -Url ($target+'/redirect') -ApiKey 'loopback-test-key'} catch {$refused=$true}
    if (-not $refused) {throw 'A redirect was followed.'}
    $putRefused=$false
    try {$null=Invoke-AssistLocalJson -Url ($target+'/redirect') -Method PUT -Body @{name=$label} -ApiKey 'loopback-test-key'} catch {$putRefused=$true}
    if (-not $putRefused) {throw 'A JSON PUT redirect was followed.'}
    $getBodyRefused=$false
    try {$null=Invoke-AssistLocalJson -Url ($target+'/health') -Body @{name=$label}} catch {$getBodyRefused=$true}
    if (-not $getBodyRefused) {throw 'A JSON body was accepted for GET.'}
    $after=(Invoke-AssistLocalJson -Url ($target+'/stats')).proxy_hits
    if ($after -ne $before) {throw 'A loopback request or redirect reached the proxy.'}
    [pscustomobject]@{passed=7;negative_control_proxy_hits=$before;proxy_hits_after_local_requests=$after;loopback_get_direct=$true;authenticated_post_direct=$true;name_only_utf8_put_direct=$true;redirect_refused=$true;put_redirect_refused=$true;get_body_refused=$true} | ConvertTo-Json
} finally {
    [Net.WebRequest]::DefaultWebProxy=$previousProxy
    $env:HTTP_PROXY=$previousHttp; $env:HTTPS_PROXY=$previousHttps; $env:NO_PROXY=$previousNoProxy
    if ($identity -and (Test-AssistProcessIdentity $identity)) {Stop-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue; $null=$fixture.WaitForExit(2000)}
    $checked=[IO.Path]::GetFullPath($base)
    $prefix=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')+'\Osaurus Assist Http-'
    if (-not $checked.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) {throw 'HTTP fixture cleanup escaped its sandbox.'}
    Assert-AssistNoReparsePoints $checked
    Remove-Item -LiteralPath $checked -Recurse -Force
}
