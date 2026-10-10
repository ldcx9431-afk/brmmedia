#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$Account = "$env:COMPUTERNAME\deploy",
    [string]$TaskName = 'BRMMedia-WSL-Keeper'
)

$ErrorActionPreference = 'Stop'
$sid = ([System.Security.Principal.NTAccount]::new($Account)).Translate(
    [System.Security.Principal.SecurityIdentifier]
).Value
$sidEntry = "*$sid"
$tempCfg = Join-Path $env:TEMP "brmmedia-user-rights-$PID.inf"
$database = Join-Path $env:WINDIR 'security\database\secedit.sdb'
$logPath = 'D:\brmmedia\artifacts\grant_deploy_batch_logon.log'

try {
    & secedit.exe /export /cfg $tempCfg /areas USER_RIGHTS | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to export the local user-rights policy.' }

    $lines = [System.Collections.Generic.List[string]](Get-Content -LiteralPath $tempCfg -Encoding Unicode)
    $updated = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match '^\s*SeBatchLogonRight\s*=\s*(.*)$') {
            $entries = @($Matches[1] -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
            if ($entries -notcontains $sidEntry) { $entries += $sidEntry }
            $lines[$index] = "SeBatchLogonRight = $($entries -join ',')"
            $updated = $true
            break
        }
    }

    if (-not $updated) {
        $section = $lines.IndexOf('[Privilege Rights]')
        if ($section -lt 0) { throw 'The exported security template has no [Privilege Rights] section.' }
        $lines.Insert($section + 1, "SeBatchLogonRight = $sidEntry")
    }

    Set-Content -LiteralPath $tempCfg -Value $lines -Encoding Unicode
    $seceditOutput = & secedit.exe /configure /db $database /cfg $tempCfg /areas USER_RIGHTS /log $logPath 2>&1
    $seceditOutput | Write-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Host "secedit diagnostic log: $logPath"
        throw 'Unable to apply the Log on as a batch job policy.'
    }

    & gpupdate.exe /target:computer /force | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to refresh the local computer policy.' }

    & schtasks.exe /Run /TN $TaskName | Write-Host
    if ($LASTEXITCODE -ne 0) { throw "The privilege was granted, but task $TaskName could not be started." }
    Write-Host "Granted 'Log on as a batch job' to $Account and started $TaskName."
} finally {
    Remove-Item -LiteralPath $tempCfg -Force -ErrorAction SilentlyContinue
}
