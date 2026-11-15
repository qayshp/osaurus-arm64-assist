Set-StrictMode -Version Latest
$script:AssistProduct = 'org.osaurus-community.arm64-assist'
$script:AssistScriptsPath = $PSScriptRoot

function Resolve-AssistRoot {
    param([string]$RootPath)
    if (-not $RootPath) { $RootPath = Join-Path $env:LOCALAPPDATA 'OsaurusArm64Assist' }
    $resolved = [IO.Path]::GetFullPath($RootPath)
    if ($resolved -eq [IO.Path]::GetPathRoot($resolved)) { throw 'An installation cannot use a drive root.' }
    $resolved = $resolved.TrimEnd('\', '/')
    Assert-AssistPathAncestors $resolved
    $protected = @($env:USERPROFILE, $env:LOCALAPPDATA, $env:APPDATA, $env:WINDIR, $env:ProgramFiles, ${env:ProgramFiles(x86)})
    foreach ($item in $protected) {
        if ($item -and $resolved.Equals([IO.Path]::GetFullPath($item).TrimEnd('\', '/'), [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Choose a dedicated installation directory, not a system or profile directory.'
        }
    }
    return $resolved
}

function Assert-AssistPathAncestors {
    param([Parameter(Mandatory=$true)][string]$Path)
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Installation paths cannot traverse symbolic links or junctions.'
            }
        }
        $parent = [IO.Directory]::GetParent($current)
        if ($null -eq $parent) { break }
        $current = $parent.FullName
    }
}

function Read-AssistJson {
    param([Parameter(Mandatory=$true)][string]$Path)
    if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) {
        return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json -DateKind String
    }
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Write-AssistJson {
    param([Parameter(Mandatory=$true)][string]$Path, [Parameter(Mandatory=$true)]$Value)
    $text = $Value | ConvertTo-Json -Depth 30
    [IO.File]::WriteAllText($Path, $text, (New-Object Text.UTF8Encoding($false)))
}

function Assert-AssistInstallation {
    param([Parameter(Mandatory=$true)][string]$RootPath)
    $root = Resolve-AssistRoot $RootPath
    $markerPath = Join-Path $root '.osaurus-arm64-assist-install.json'
    Assert-AssistPathAncestors $markerPath
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) { throw "No owned installation marker at $root" }
    $marker = Read-AssistJson $markerPath
    if ($marker.product -ne $script:AssistProduct -or -not $marker.root.Equals($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The installation ownership marker does not match this directory.'
    }
    return $root
}

function Get-AssistPython {
    param([string]$PythonPath)
    $candidates = @()
    if ($PythonPath) { $candidates += $PythonPath }
    else {
        $command = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($command -and $command.Source -notlike '*\Microsoft\WindowsApps\python.exe') { $candidates += $command.Source }
        $pythonRoot = Join-Path $env:LOCALAPPDATA 'Programs/Python'
        if (Test-Path -LiteralPath $pythonRoot) {
            $candidates += @(Get-ChildItem -LiteralPath $pythonRoot -Directory | ForEach-Object { Join-Path $_.FullName 'python.exe' })
        }
    }
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            $description = & $candidate -I (Join-Path $script:AssistScriptsPath 'python_probe.py') 2>$null
            if ($LASTEXITCODE -ne 0) { continue }
            $info = $description | ConvertFrom-Json
            if ($info.machine -in @('ARM64','aarch64') -and ($info.version[0] -gt 3 -or ($info.version[0] -eq 3 -and $info.version[1] -ge 11))) {
                return [IO.Path]::GetFullPath($info.executable)
            }
        } catch { }
    }
    throw 'Native ARM64 CPython 3.11 or newer is required. Install an ARM64 build from https://www.python.org/downloads/windows/ (user install), then rerun with -PythonPath <full path to python.exe>. An x64 Python running under emulation is not compatible with these ARM64 DLLs.'
}

function Invoke-AssistPython {
    param([Parameter(Mandatory=$true)][string]$Python, [Parameter(Mandatory=$true)][string[]]$Arguments)
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }
}

function Quote-AssistArgument {
    param([Parameter(Mandatory=$true)][AllowEmptyString()][string]$Value)
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Get-AssistProcessIdentity {
    param([Parameter(Mandatory=$true)][int]$ProcessId)
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $cim -or -not $cim.CommandLine) { return $null }
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($cim.CommandLine))).Replace('-','').ToLowerInvariant() }
    finally { $algorithm.Dispose() }
    return [pscustomobject]@{
        pid=$ProcessId; start_time_utc=$process.StartTime.ToUniversalTime().ToString('o')
        executable=$cim.ExecutablePath; command_line_sha256=$hash; command_line=$cim.CommandLine
    }
}

function Test-AssistProcessIdentity {
    param([Parameter(Mandatory=$true)]$Identity)
    $current = Get-AssistProcessIdentity ([int]$Identity.pid)
    if (-not $current) { return $false }
    return ((Get-AssistTimeTicks $current.start_time_utc) -eq (Get-AssistTimeTicks $Identity.start_time_utc) -and
        $current.executable -eq $Identity.executable -and $current.command_line_sha256 -eq $Identity.command_line_sha256)
}

function Get-AssistTimeTicks {
    param([Parameter(Mandatory=$true)]$Value)
    if ($Value -is [DateTime]) { return $Value.ToUniversalTime().Ticks }
    return [DateTime]::Parse([string]$Value,[Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime().Ticks
}

function Test-AssistServerIdentity {
    param([Parameter(Mandatory=$true)]$Identity, [Parameter(Mandatory=$true)][string]$RootPath)
    if (-not (Test-AssistProcessIdentity $Identity)) { return $false }
    $current = Get-AssistProcessIdentity ([int]$Identity.pid)
    $command = $current.command_line.Replace('"','')
    $config = Join-Path $RootPath 'config.json'
    return ([regex]::IsMatch($command, '(?i)(?:^|\s)-m\s+osaurus_arm64_assist\s+serve(?:\s|$)') -and
        $command.IndexOf($config,[StringComparison]::OrdinalIgnoreCase) -ge 0)
}

function Get-AssistHealth {
    param([Parameter(Mandatory=$true)][int]$Port)
    try { return Invoke-AssistLocalJson -Url "http://127.0.0.1:$Port/health" -TimeoutSec 2 }
    catch { return $null }
}

function Invoke-AssistLocalJson {
    param([Parameter(Mandatory=$true)][string]$Url, [ValidateSet('GET','POST')][string]$Method='GET',
        [string]$ApiKey, [ValidateRange(1,60)][int]$TimeoutSec=2)
    $uri = [Uri]$Url
    if ($uri.Scheme -ne 'http' -or -not $uri.IsLoopback -or $uri.UserInfo) { throw 'Companion HTTP requests must use a local loopback address.' }
    foreach ($address in [Net.Dns]::GetHostAddresses($uri.DnsSafeHost)) {
        if (-not [Net.IPAddress]::IsLoopback($address)) { throw 'The local hostname resolved outside loopback.' }
    }
    $request = [Net.HttpWebRequest]::Create($uri)
    # Never send local credentials to a system/environment proxy or a redirected URL.
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.UseDefaultCredentials = $false
    $request.Credentials = $null
    $request.Method = $Method
    $request.Timeout = $TimeoutSec * 1000
    $request.ReadWriteTimeout = $TimeoutSec * 1000
    $request.Accept = 'application/json'
    if ($ApiKey) { $request.Headers['Authorization'] = 'Bearer ' + $ApiKey }
    if ($Method -eq 'POST') { $request.ContentLength = 0 }
    $response = $null; $reader = $null
    try {
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -lt 200 -or [int]$response.StatusCode -ge 300) { throw 'The local endpoint returned a non-success status; redirects are not followed.' }
        $reader = New-Object IO.StreamReader($response.GetResponseStream(),[Text.Encoding]::UTF8,$true)
        $text = $reader.ReadToEnd()
        if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) { return $text | ConvertFrom-Json -DateKind String }
        return $text | ConvertFrom-Json
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
    }
}

function Assert-AssistRuntime {
    param([Parameter(Mandatory=$true)][string]$RuntimePath, [Parameter(Mandatory=$true)]$Lock)
    Assert-AssistPathAncestors $RuntimePath
    Assert-AssistNoReparsePoints $RuntimePath
    foreach ($file in $Lock.files) {
        $path = Join-Path $RuntimePath $file.path
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Runtime file missing: $($file.path)" }
        $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $file.sha256) { throw "Runtime integrity check failed: $($file.path). Reinstall the pinned runtime." }
    }
    $native = Join-Path $RuntimePath $Lock.native_subdirectory
    foreach ($file in Get-ChildItem -LiteralPath $native -File) {
        if ($file.Extension -in @('.dll','.so','.cat','.inf')) {
            $relative = $Lock.native_subdirectory.TrimEnd('/') + '/' + $file.Name
            if ($relative -notin @($Lock.files | ForEach-Object {$_.path})) { throw "Unexpected native runtime file: $($file.Name). Clean reinstall required." }
        }
    }
}

function Assert-AssistZipPaths {
    param([Parameter(Mandatory=$true)][string]$Archive, [Parameter(Mandatory=$true)][string]$Destination)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $prefix = [IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $full = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
            if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Archive contains an entry outside the extraction directory.' }
        }
    } finally { $zip.Dispose() }
}

function Assert-AssistNoReparsePoints {
    param([Parameter(Mandatory=$true)][string]$Path)
    $pending = New-Object 'Collections.Generic.Stack[string]'
    $pending.Push([IO.Path]::GetFullPath($Path))
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        $item = Get-Item -LiteralPath $current -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'An owned installation contains a junction or symbolic link; refusing recursive removal.'
        }
        if ($item.PSIsContainer) {
            foreach ($child in Get-ChildItem -LiteralPath $current -Force) { $pending.Push($child.FullName) }
        }
    }
}

function Assert-AssistModelsOutsideInstallation {
    param([Parameter(Mandatory=$true)][string]$RootPath)
    $root = [IO.Path]::GetFullPath($RootPath).TrimEnd('\')
    $configPath = Join-Path $root 'config.json'
    if (Test-Path -LiteralPath $configPath) {
        Assert-AssistPathAncestors $configPath
        $config = Read-AssistJson $configPath
        if ($config.PSObject.Properties['models']) {
            foreach ($model in @($config.models)) {
                $path = [IO.Path]::GetFullPath([string]$model.path)
                if ($path.Equals($root,[StringComparison]::OrdinalIgnoreCase) -or $path.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)) {
                    throw 'A configured model is inside the installation. Move model weights outside the installation and update its profile before reinstalling or uninstalling. No model was removed.'
                }
            }
        }
    }
    Assert-AssistNoReparsePoints $root
    if (Get-ChildItem -LiteralPath $root -Filter '*.gguf' -File -Recurse | Select-Object -First 1) {
        throw 'A GGUF file is inside the installation. Move model weights outside the installation before reinstalling or uninstalling. No model was removed.'
    }
}

function Get-AssistOsaurus {
    $command = Get-Command osaurus.exe -ErrorAction SilentlyContinue
    if (-not $command) { throw 'Osaurus CLI was not found. Install Osaurus for Windows, launch it once, then retry.' }
    return $command.Source
}

function Read-AssistOsaurusExport {
    param([Parameter(Mandatory=$true)][string]$Osaurus)
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('osaurus-assist-' + [guid]::NewGuid().ToString() + '.json')
    try {
        $null = & $Osaurus config export -o $temporary --format json
        if ($LASTEXITCODE -ne 0) { throw 'Could not export Osaurus configuration. Start Osaurus and retry.' }
        return Read-AssistJson $temporary
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}

function Get-AssistProviderType {
    param([string]$Value)
    if ($Value -in @('custom','openaicompatible')) { return 'openaicompatible' }
    return $Value.ToLowerInvariant()
}

function Assert-AssistOwnedProvider {
    param([Parameter(Mandatory=$true)]$Provider, [Parameter(Mandatory=$true)]$Registration)
    if ($Provider.name -ine $Registration.provider_name -or
        (Get-AssistProviderType $Provider.provider) -ne $Registration.provider_type -or
        $Provider.base_url.TrimEnd('/') -cne $Registration.base_url.TrimEnd('/') -or
        [bool]$Provider.enabled -ne [bool]$Registration.enabled) {
        throw 'The Osaurus provider was edited after registration. It was preserved; restore its recorded settings or remove it manually before disconnecting.'
    }
}
