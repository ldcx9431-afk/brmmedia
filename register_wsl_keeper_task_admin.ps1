#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$RunAsUser = "$env:COMPUTERNAME\deploy"
)

$ErrorActionPreference = 'Stop'
$taskName = 'BRMMedia-WSL-Keeper'
$wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
$arguments = '-d BRMMedia-Ubuntu -u root -- /bin/bash /srv/brmmedia/wsl_keepalive.sh'
$action = "`"$wsl`" $arguments"
$credential = Get-Credential -UserName $RunAsUser -Message 'Enter the Windows deploy account password for the BRMMedia WSL keeper task'
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($credential.Password)
try {
    $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)

    # WSL distributions are registered per Windows user, so this task must run
    # as deploy (not SYSTEM). ONSTART + stored task credentials make it survive
    # logout and remain independent of the SSH session that created it.
    & schtasks.exe /Create /TN $taskName /TR $action /SC ONSTART /RU $credential.UserName /RP $password /RL HIGHEST /F | Write-Host
    if ($LASTEXITCODE -ne 0) { throw "Unable to register scheduled task $taskName" }
} finally {
    if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    Remove-Variable password -ErrorAction SilentlyContinue
}

& schtasks.exe /Run /TN $taskName | Write-Host
if ($LASTEXITCODE -ne 0) { throw "Unable to start scheduled task $taskName" }

Write-Host "Registered and started scheduled task: $taskName"
