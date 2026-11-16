[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$base=Join-Path ([IO.Path]::GetTempPath()) ('Osaurus Assist Provider-'+[guid]::NewGuid().ToString())
$root=Join-Path $base 'owned'
$passed=New-Object 'Collections.Generic.List[string]'
$installation=[guid]::NewGuid().ToString()
$statePath=Join-Path $root 'state/osaurus-registration.json'
function Assert-Test {param([bool]$Condition,[string]$Name) if(-not $Condition){throw "Provider assertion failed: $Name"};$passed.Add($Name)}
function Assert-Refusal {param([scriptblock]$Action,[string]$Name) $refused=$false;try{& $Action|Out-Null}catch{$refused=$true};Assert-Test $refused $Name}
function Copy-Fixture {param($Value) return ($Value|ConvertTo-Json -Depth 30|ConvertFrom-Json)}
function Reset-Fixture {
    param([string]$Name='Osaurus ARM64 Assist')
    $script:fixture=[pscustomobject]@{
        puts=0;gets=0;fail='';
        views=@([pscustomobject]@{id='prov-owned';name=$Name;type='openaicompatible';baseUrl='http://127.0.0.1:8082/v1';enabled=$true;hasApiKey=$true;apiKeyLast4='TEST';defaultModel='raptor-fixture';customHeaders=@{}},
            [pscustomobject]@{id='prov-other';name='Other provider';type='openaicompatible';baseUrl='http://127.0.0.1:1338/v1';enabled=$true;hasApiKey=$true});
        export=[pscustomobject]@{version=1;providers=@([pscustomobject]@{name=$Name;provider='custom';base_url='http://127.0.0.1:8082/v1';enabled=$true;set_api_key=$false},
            [pscustomobject]@{name='Other provider';provider='custom';base_url='http://127.0.0.1:1338/v1';enabled=$true;set_api_key=$false});
            agents=@([pscustomobject]@{id='agent-fixture';model='prov-owned/raptor-fixture'});default_agent=[pscustomobject]@{model='prov-owned/raptor-fixture'}}
    }
    $registration=[pscustomobject]@{version=1;installation_id=$installation;provider_name=$Name;provider_type='openaicompatible';base_url='http://127.0.0.1:8082/v1';enabled=$true;key_ref=('keychain:osaurus-arm64-assist/'+$installation);credential_created=$true;status='connected';provider_id='prov-owned';model_ids=@('raptor-fixture','ornith-fixture');created_utc='2026-01-01T00:00:00Z'}
    Write-AssistProviderRegistration $statePath $registration
    return $registration
}
function Read-AssistOsaurusExport {param([string]$Osaurus) return (Copy-Fixture $script:fixture.export)}
function Invoke-AssistLocalJson {
    param([string]$Url,[string]$Method='GET',$Body,[int]$TimeoutSec)
    if($Method -eq 'GET'){
        $script:fixture.gets++
        if($Url -cne 'http://127.0.0.1:1337/admin/providers'){throw 'Unexpected fixture GET.'}
        return [pscustomobject]@{providers=@($script:fixture.views|ForEach-Object{Copy-Fixture $_})}
    }
    $script:fixture.puts++
    if($Method -cne 'PUT' -or $Url -cne 'http://127.0.0.1:1337/admin/providers/prov-owned' -or
        @($Body.Keys).Count -ne 1 -or -not $Body.ContainsKey('name')){throw 'Rename must send only name to the original ID.'}
    if($script:fixture.fail -eq 'before'){throw 'Synthetic connection failure before update.'}
    $script:fixture.views[0].name=$Body.name
    $script:fixture.export.providers[0].name=$Body.name
    if($script:fixture.fail -eq 'after'){throw 'Synthetic lost response after update.'}
    if($script:fixture.fail -eq 'edited'){$script:fixture.views[0].defaultModel='unexpected-concurrent-edit'}
    return (Copy-Fixture $script:fixture.views[0])
}
function Rename-Fixture {param($Registration,[string]$Name='NPU Hybrid',[switch]$ResolveOnly) Set-AssistOwnedProviderName -RootPath $root -Osaurus 'fixture-cli' -OsaurusUrl 'http://127.0.0.1:1337' -Registration $Registration -Name $Name -ResolveOnly:$ResolveOnly}
try{
    New-Item -ItemType Directory -Path (Join-Path $root 'state') -Force|Out-Null
    Write-AssistJson (Join-Path $root '.osaurus-arm64-assist-install.json') @{product=$script:AssistProduct;root=$root;installation_id=$installation}
    Assert-Test ((Get-AssistProviderName) -ceq 'NPU Hybrid') 'fresh default fits the concise provider badge'
    $reg=Reset-Fixture
    Assert-Test ((Get-AssistProviderName $reg) -ceq 'NPU Hybrid') 'legacy default selects automatic migration'
    $before=Copy-Fixture $script:fixture.export
    $updated=Rename-Fixture $reg
    Assert-Test ($script:fixture.puts -eq 1 -and $script:fixture.views.Count -eq 2 -and $updated.provider_id -ceq 'prov-owned' -and $updated.provider_name -ceq 'NPU Hybrid') 'rename updates the same provider without duplicates'
    Assert-Test ($updated.key_ref -ceq $reg.key_ref -and ($updated.model_ids -join ',') -ceq ($reg.model_ids -join ',') -and $updated.credential_created -eq $true) 'credential reference and model IDs survive migration'
    Assert-Test (($before.agents|ConvertTo-Json -Compress) -ceq ($script:fixture.export.agents|ConvertTo-Json -Compress) -and ($before.default_agent|ConvertTo-Json -Compress) -ceq ($script:fixture.export.default_agent|ConvertTo-Json -Compress)) 'agent and default-agent references survive migration'
    Assert-Test (($before.providers[1]|ConvertTo-Json -Compress) -ceq ($script:fixture.export.providers[1]|ConvertTo-Json -Compress)) 'unrelated provider survives migration'
    $again=Rename-Fixture $updated
    Assert-Test ($script:fixture.puts -eq 1 -and -not $again.PSObject.Properties['provider_name_pending']) 'reconnecting is idempotent'
    $custom=Reset-Fixture 'My local models'
    Assert-Test ((Get-AssistProviderName $custom) -ceq 'My local models') 'ordinary upgrade preserves a custom ProviderName'
    $same=Rename-Fixture $custom (Get-AssistProviderName $custom)
    Assert-Test ($script:fixture.puts -eq 0 -and $same.provider_name -ceq 'My local models') 'custom label requires no API update'
    $explicit=Get-AssistProviderName -Registration $custom -RequestedName 'NPU Hybrid' -ExplicitName $true
    $null=Rename-Fixture $custom $explicit
    Assert-Test ($script:fixture.puts -eq 1) 'explicit custom label change uses the same migration path'
    $reg=Reset-Fixture;$reg.status='pending'
    Assert-Test ((Get-AssistProviderName $reg) -ceq 'Osaurus ARM64 Assist') 'interrupted initial registration retains its recorded name'
    $reg=Reset-Fixture;$reg.provider_id=$null
    $updated=Rename-Fixture $reg
    Assert-Test ($updated.provider_id -ceq 'prov-owned' -and $script:fixture.puts -eq 1) 'older registration resolves a unique owned API ID before rename'
    foreach($change in @('collision','wrong-id','url','type','disabled','missing-key','foreign-install','foreign-key')){
        $reg=Reset-Fixture
        switch($change){
            'collision'{$script:fixture.views[1].name='NPU Hybrid';$script:fixture.export.providers[1].name='NPU Hybrid'}
            'wrong-id'{$reg.provider_id='prov-foreign'}
            'url'{$script:fixture.views[0].baseUrl='http://127.0.0.1:9999/v1'}
            'type'{$script:fixture.views[0].type='anthropic'}
            'disabled'{$script:fixture.views[0].enabled=$false}
            'missing-key'{$script:fixture.views[0].hasApiKey=$false}
            'foreign-install'{$reg.installation_id=[guid]::NewGuid().ToString()}
            'foreign-key'{$reg.key_ref='keychain:unrelated'}
        }
        Assert-Refusal {Rename-Fixture $reg} ($change+' is refused')
        Assert-Test ($script:fixture.puts -eq 0) ($change+' leaves provider untouched')
    }
    foreach($failure in @('before','after')){
        $reg=Reset-Fixture;$script:fixture.fail=$failure
        Assert-Refusal {Rename-Fixture $reg} ('interrupted rename '+$failure+' is reported')
        $pending=Read-AssistJson $statePath
        Assert-Test ($pending.provider_name -ceq 'Osaurus ARM64 Assist' -and $pending.provider_name_pending -ceq 'NPU Hybrid' -and $pending.key_ref -ceq $reg.key_ref) ('interrupted rename '+$failure+' retains recoverable ownership')
        $script:fixture.fail=''
        $updated=Rename-Fixture $pending (Get-AssistProviderName $pending)
        $expectedPuts=if($failure -eq 'before'){2}else{1}
        Assert-Test ($updated.provider_name -ceq 'NPU Hybrid' -and -not $updated.PSObject.Properties['provider_name_pending'] -and $script:fixture.puts -eq $expectedPuts) ('interrupted rename '+$failure+' resumes safely')
    }
    foreach($applied in @($false,$true)){
        $reg=Reset-Fixture
        $reg|Add-Member -NotePropertyName provider_name_pending -NotePropertyValue 'NPU Hybrid'
        $reg|Add-Member -NotePropertyName provider_rename_signature -NotePropertyValue (Get-AssistProviderViewSignature $script:fixture.views[0])
        if($applied){$script:fixture.views[0].name='NPU Hybrid';$script:fixture.export.providers[0].name='NPU Hybrid'}
        $resolved=Rename-Fixture $reg 'Osaurus ARM64 Assist' -ResolveOnly
        $expectedName=if($applied){'NPU Hybrid'}else{'Osaurus ARM64 Assist'}
        Assert-Test ($resolved.provider_name -ceq $expectedName -and $script:fixture.puts -eq 0 -and -not $resolved.PSObject.Properties['provider_name_pending']) ('disconnect resolves pending ownership applied='+$applied+' without renaming')
    }
    $reg=Reset-Fixture;$script:fixture.fail='edited'
    Assert-Refusal {Rename-Fixture $reg} 'unexpected post-update changes fail verification'
    Assert-Test ([bool](Read-AssistJson $statePath).PSObject.Properties['provider_name_pending']) 'verification failure keeps pending ownership instead of blind rollback'
    $script:fixture.fail=''
    $pending=Read-AssistJson $statePath
    Assert-Refusal {Rename-Fixture $pending} 'retry preserves unexpected concurrent edits for review'
    Assert-Test ($script:fixture.puts -eq 1) 'retry does not overwrite a concurrent edit'
    [pscustomobject]@{passed=$passed.Count;checks=@($passed)}|ConvertTo-Json -Depth 4
}finally{
    if(Test-Path -LiteralPath $base){
        $checked=[IO.Path]::GetFullPath($base)
        $prefix=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')+'\Osaurus Assist Provider-'
        if(-not $checked.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)){throw 'Provider fixture cleanup escaped its sandbox.'}
        Assert-AssistNoReparsePoints $checked
        Remove-Item -LiteralPath $checked -Recurse -Force
    }
}
