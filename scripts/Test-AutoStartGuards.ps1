[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$base = Join-Path ([IO.Path]::GetTempPath()) ('Osaurus Assist Automatic ' + [guid]::NewGuid().ToString())
$root = Join-Path $base 'owned install'
$startup = Join-Path $base 'fake Startup'
$outside = Join-Path $base 'outside'
$passed = New-Object 'Collections.Generic.List[string]'
$helper = $null; $helperIdentity = $null; $lockHelper = $null; $lockHelperIdentity = $null; $junction = $null
function Assert-Test { param([bool]$Condition,[string]$Name) if (-not $Condition) { throw "Automatic lifecycle assertion failed: $Name" }; $passed.Add($Name) }
function Assert-Refusal { param([scriptblock]$Action,[string]$Name) $refused=$false; try { & $Action | Out-Null } catch { $refused=$true }; Assert-Test $refused $Name }
function Edit-FixtureShortcut {
    param([string]$Path,[string]$Property,[string]$Value)
    $shell=New-Object -ComObject WScript.Shell
    $shortcut=$shell.CreateShortcut($Path)
    try { $shortcut.$Property=$Value; $shortcut.Save() }
    finally { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shortcut); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell) }
}
try {
    New-Item -ItemType Directory -Force -Path $root,$startup,$outside,(Join-Path $root 'state'),(Join-Path $root '.venv/Scripts') | Out-Null
    $installation = [guid]::NewGuid().ToString()
    Write-AssistJson (Join-Path $root '.osaurus-arm64-assist-install.json') @{product=$script:AssistProduct;root=$root;installation_id=$installation}
    Write-AssistJson (Join-Path $root 'config.json') @{schema_version=1;auto_start=$false;models=@();port=65000;api_key='PRIVATE_FIXTURE_CANARY'}
    [IO.File]::WriteAllBytes((Join-Path $root '.venv/Scripts/pythonw.exe'),[byte[]]@(0,1))
    $sentinel = Join-Path $outside 'preserve.txt'; [IO.File]::WriteAllText($sentinel,'outside sentinel')
    $descriptor=Get-AssistAutoStartDescriptor $root $startup
    Assert-Test ($descriptor.shortcut_path.StartsWith($startup+'\',[StringComparison]::OrdinalIgnoreCase)) 'shortcut is confined to requested fixture Startup folder'
    Assert-Test ($descriptor.arguments -like '*"-I" "-m" "osaurus_arm64_assist" "watch" "--config"*') 'hidden watcher arguments use isolated Python and quoted config'
    Assert-Test ($descriptor.arguments -notlike '*PRIVATE*') 'shortcut arguments contain no API key'
    Assert-Test (-not (Assert-AssistAutoRegistration $root $startup)) 'absent registration is harmless'
    $registration=Register-AssistAutoStart $root $startup
    Assert-Test (Test-Path -LiteralPath $registration.shortcut_path -PathType Leaf) 'owned shortcut is created in fixture folder'
    Assert-Test ([bool](Assert-AssistAutoRegistration $root $startup)) 'COM shortcut properties match ownership tracking'
    $beforeHash=(Get-FileHash -LiteralPath $registration.shortcut_path).Hash
    $beforeTime=(Get-Item -LiteralPath $registration.shortcut_path).LastWriteTimeUtc.Ticks
    $null=Register-AssistAutoStart $root $startup
    Assert-Test ((Get-FileHash -LiteralPath $registration.shortcut_path).Hash -eq $beforeHash -and (Get-Item -LiteralPath $registration.shortcut_path).LastWriteTimeUtc.Ticks -eq $beforeTime) 'repeated registration leaves existing shortcut untouched'
    foreach ($edit in @(@{name='TargetPath';value=(Join-Path $outside 'foreign.exe')},@{name='Arguments';value='--foreign'},@{name='WorkingDirectory';value=$outside},@{name='Description';value='foreign ownership'})) {
        Edit-FixtureShortcut $registration.shortcut_path $edit.name $edit.value
        Assert-Refusal { Unregister-AssistAutoStart $root $startup } ('edited '+$edit.name+' is preserved')
        Assert-Test (Test-Path -LiteralPath $registration.shortcut_path) ('edited '+$edit.name+' shortcut remains present')
        $propertyMap=@{TargetPath='target_path';Arguments='arguments';WorkingDirectory='working_directory';Description='description'}
        Edit-FixtureShortcut $registration.shortcut_path $edit.name $registration.($propertyMap[$edit.name])
    }
    $statePath=Join-Path $root 'state/automatic-registration.json'
    $badRecord=Read-AssistJson $statePath; $badRecord.shortcut_path=$sentinel; Write-AssistJson $statePath $badRecord
    Assert-Refusal { Unregister-AssistAutoStart $root $startup } 'foreign path in ownership state cannot redirect removal'
    Assert-Test ((Get-Content -LiteralPath $sentinel -Raw) -eq 'outside sentinel') 'foreign target survives ownership refusal'
    Write-AssistJson $statePath $descriptor
    Unregister-AssistAutoStart $root $startup
    Assert-Test (-not (Test-Path -LiteralPath $descriptor.shortcut_path) -and -not (Test-Path -LiteralPath $statePath)) 'only owned shortcut and registration are removed'
    Edit-FixtureShortcut $descriptor.shortcut_path 'TargetPath' (Join-Path $outside 'foreign.exe')
    Assert-Refusal { Register-AssistAutoStart $root $startup } 'untracked existing shortcut is not adopted'
    Remove-Item -LiteralPath $descriptor.shortcut_path -Force
    $junction=Join-Path $base 'Startup junction'
    New-Item -ItemType Junction -Path $junction -Target $startup | Out-Null
    Assert-Refusal { Get-AssistAutoStartDescriptor $root $junction } 'Startup ancestor junction is refused'
    [IO.Directory]::Delete($junction); $junction=$null
    Set-AssistAutoStartConfig $root $true
    $config=Read-AssistJson (Join-Path $root 'config.json')
    Assert-Test ($config.auto_start -eq $true -and $config.api_key -eq 'PRIVATE_FIXTURE_CANARY') 'atomic config toggle preserves unrelated settings and token'
    Set-AssistAutoStartConfig $root $false
    Assert-Test ((Read-AssistJson (Join-Path $root 'config.json')).auto_start -eq $false) 'automatic config can be disabled'
    $beforeConfig=(Get-FileHash -LiteralPath (Join-Path $root 'config.json')).Hash
    & (Join-Path $PSScriptRoot 'Enable-AutoStart.ps1') -RootPath $root -WhatIf
    & (Join-Path $PSScriptRoot 'Disable-AutoStart.ps1') -RootPath $root -WhatIf
    Assert-Test ((Get-FileHash -LiteralPath (Join-Path $root 'config.json')).Hash -eq $beforeConfig -and -not (Test-Path -LiteralPath $statePath)) 'public WhatIf operations do not register or edit config'

    $idle=Join-Path $base 'idle.ps1'; [IO.File]::WriteAllText($idle,'Start-Sleep -Seconds 60')
    $powershell=(Get-Process -Id $PID).Path
    $helper=Start-Process -FilePath $powershell -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File '+(Quote-AssistArgument $idle)) -WindowStyle Hidden -PassThru
    $helperIdentity=Get-AssistProcessIdentity $helper.Id
    $watchState=@{product=$script:AssistProduct;installation_id=$installation;instance_id=[guid]::NewGuid().ToString();pid=$helper.Id;
        creation_filetime=$helper.StartTime.ToFileTimeUtc();session_id=$helper.SessionId;config_path=(Join-Path $root 'config.json');status='fixture';app_state='absent';last_error=$null}
    Assert-Test (-not (Test-AssistSupervisorIdentity ([pscustomobject]$watchState) $root)) 'same-PID same-time foreign executable is rejected as watcher'
    Write-AssistJson (Join-Path $root 'state/supervisor.json') $watchState
    Assert-Refusal { & (Join-Path $PSScriptRoot 'Stop-AutoStart.ps1') -RootPath $root } 'stop preserves matching live foreign process'
    Assert-Test (-not $helper.HasExited) 'foreign process remains alive after watcher stop refusal'
    Set-AssistAutoStartConfig $root $true
    Assert-Refusal { & (Join-Path $PSScriptRoot 'Start-AutoStart.ps1') -RootPath $root } 'watcher start refuses live conflicting tracking'
    $watchState.creation_filetime=[long]$watchState.creation_filetime-1
    Write-AssistJson (Join-Path $root 'state/supervisor.json') $watchState
    & (Join-Path $PSScriptRoot 'Stop-AutoStart.ps1') -RootPath $root
    Assert-Test (-not $helper.HasExited -and -not (Test-Path -LiteralPath (Join-Path $root 'state/supervisor.json'))) 'stale watcher tracking is removed without killing reused PID'
    Write-AssistJson (Join-Path $root 'state/server.json') @{identity=$helperIdentity;instance_id='fixture';launcher_identity=$null;port=65000}
    & (Join-Path $PSScriptRoot 'Stop.ps1') -RootPath $root -ServerOnly -AutomaticOwnerId $installation
    Assert-Test (-not $helper.HasExited -and (Test-Path -LiteralPath (Join-Path $root 'state/server.json'))) 'automatic stop preserves independently managed server'
    Assert-Refusal { & (Join-Path $PSScriptRoot 'Start.ps1') -RootPath $root -AutomaticOwnerId $installation } 'automatic start refuses to adopt manual server'
    Assert-Refusal { Get-AssistManualServerTransferRequired $root } 'automatic enable refuses foreign live manual server before any mutation'
    Assert-Refusal { & (Join-Path $PSScriptRoot 'Stop.ps1') -RootPath $root -ServerOnly -AutomaticOwnerId ([guid]::NewGuid().ToString()) } 'foreign automatic owner UUID is refused'
    Remove-Item -LiteralPath (Join-Path $root 'state/server.json') -Force

    & {
        $nativeCurrent=Get-Process -Id $PID
        $mockStarted=[DateTime]::UtcNow.AddMinutes(-1)
        $mockProcessSession=$nativeCurrent.SessionId
        $mockState=[pscustomobject]@{product=$script:AssistProduct;installation_id=$installation;instance_id=[guid]::NewGuid().ToString();pid=50000000;
            creation_filetime=$mockStarted.ToFileTimeUtc();session_id=$nativeCurrent.SessionId;config_path=(Join-Path $root 'config.json')}
        function Get-Process { param([int]$Id) if ($Id -eq 50000000) { return [pscustomobject]@{StartTime=$mockStarted;SessionId=$mockProcessSession} }; return $nativeCurrent }
        function Get-AssistProcessIdentity { param([int]$ProcessId) return [pscustomobject]@{executable='C:\fixture\pythonw.exe';command_line=('pythonw.exe -I -m osaurus_arm64_assist watch --config "'+(Join-Path $root 'config.json')+'"')} }
        Assert-Test (Test-AssistSupervisorIdentity $mockState $root) 'native watcher schema and exact FILETIME are accepted'
        $mockState.creation_filetime=[long]$mockState.creation_filetime+1
        Assert-Test (-not (Test-AssistSupervisorIdentity $mockState $root)) 'one 100ns FILETIME difference rejects PID identity'
        $mockState.creation_filetime=$mockStarted.ToFileTimeUtc(); $mockState.session_id=$nativeCurrent.SessionId+1
        Assert-Test (-not (Test-AssistSupervisorIdentity $mockState $root)) 'another Windows session watcher is rejected'
        $mockState.session_id=$nativeCurrent.SessionId; $mockState.config_path=(Join-Path $outside 'config.json')
        Assert-Test (-not (Test-AssistSupervisorIdentity $mockState $root)) 'different config path watcher is rejected'
        function Test-AssistProcessIdentity { param($Identity) return $true }
        function Test-AssistServerIdentity { param($Identity,$RootPath) return $true }
        Write-AssistJson (Join-Path $root 'state/server.json') @{identity=@{pid=50000000}}
        Assert-Test (Get-AssistManualServerTransferRequired $root) 'verified current-session manual server can be explicitly transferred'
        $mockProcessSession=$nativeCurrent.SessionId+1
        Assert-Refusal { Get-AssistManualServerTransferRequired $root } 'verified manual server in another session cannot be transferred'
        $mockProcessSession=$nativeCurrent.SessionId
        Write-AssistJson (Join-Path $root 'state/server.json') @{identity=@{pid=50000000};automatic_owner_id=$installation}
        Assert-Test (-not (Get-AssistManualServerTransferRequired $root)) 'repeated enable does not restart an automatically owned server'
        Remove-Item -LiteralPath (Join-Path $root 'state/server.json') -Force
    }

    $ready=Join-Path $base 'lock-ready.txt'
    $lockScript=Join-Path $base 'lock.ps1'
    $commonPath=Join-Path $PSScriptRoot 'Common.ps1'
    $lockCode='param($Common,$Root,$Ready)' + "`r`n" + '. $Common' + "`r`n" + '$held=Enter-AssistOperationLock $Root Server' + "`r`n" + 'try { [IO.File]::WriteAllText($Ready,"ready"); Start-Sleep -Seconds 2 } finally { Exit-AssistOperationLock $held }'
    [IO.File]::WriteAllText($lockScript,$lockCode)
    $lockArgs=@('-NoProfile','-ExecutionPolicy','Bypass','-File',$lockScript,$commonPath,$root,$ready)
    $lockHelper=Start-Process -FilePath $powershell -ArgumentList ((@($lockArgs | ForEach-Object { Quote-AssistArgument $_ })) -join ' ') -WindowStyle Hidden -PassThru
    $lockHelperIdentity=Get-AssistProcessIdentity $lockHelper.Id
    $clock=[Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Path -LiteralPath $ready) -and $clock.Elapsed.TotalSeconds -lt 10) { Start-Sleep -Milliseconds 50 }
    Assert-Test (Test-Path -LiteralPath $ready) 'independent lock fixture entered server lifecycle mutex'
    $contender=New-Object Threading.Mutex($false,('Local\OsaurusArm64Assist.Server.'+$installation))
    try { $acquired=$contender.WaitOne(50); Assert-Test (-not $acquired) 'server lifecycle mutex serializes independent processes'; if ($acquired) { $contender.ReleaseMutex() } } finally { $contender.Dispose() }
    Assert-Test ((Get-Content -LiteralPath $sentinel -Raw) -eq 'outside sentinel') 'all external fixture data is preserved'
    [pscustomobject]@{passed=$passed.Count;checks=@($passed)} | ConvertTo-Json -Depth 4
} finally {
    foreach ($identity in @($helperIdentity,$lockHelperIdentity)) { if ($identity -and (Test-AssistProcessIdentity $identity)) { Stop-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue } }
    if ($junction -and (Test-Path -LiteralPath $junction)) { [IO.Directory]::Delete($junction) }
    if (Test-Path -LiteralPath $base) {
        $checked=[IO.Path]::GetFullPath($base)
        $prefix=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')+'\Osaurus Assist Automatic '
        if (-not $checked.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { throw 'Automatic fixture cleanup escaped its temporary directory.' }
        Assert-AssistNoReparsePoints $checked
        Remove-Item -LiteralPath $checked -Recurse -Force
    }
}
