[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$taskName = 'BRMMedia-WSL-Keeper'
$wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
$arguments = '-d BRMMedia-Ubuntu -u root -- /bin/bash /srv/brmmedia/wsl_keepalive.sh'
$action = "`"$wsl`" $arguments"

# A user-level task is deliberately used here: it does not require elevation
# and is independent of the remote SSH session that created it.
& schtasks.exe /Create /TN $taskName /TR $action /SC ONLOGON /RU $env:USERNAME /RL LIMITED /F | Write-Host
if ($LASTEXITCODE -ne 0) { throw "Unable to register scheduled task $taskName" }

& schtasks.exe /Run /TN $taskName | Write-Host
if ($LASTEXITCODE -ne 0) { throw "Unable to start scheduled task $taskName" }

Write-Host "Registered and started scheduled task: $taskName"
