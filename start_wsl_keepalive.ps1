[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$distro = 'BRMMedia-Ubuntu'
$marker = '/srv/brmmedia/wsl_keepalive.sh'
try {
    $alreadyRunning = Get-CimInstance Win32_Process -Filter "Name = 'wsl.exe'" |
        Where-Object { $_.CommandLine -like "*$marker*" }
} catch {
    # Standard users cannot inspect all Windows processes. Starting a second
    # keeper is harmless, and keeps this script usable without elevation.
    $alreadyRunning = $null
}

if ($alreadyRunning) {
    Write-Host "WSL keeper already running (PID $($alreadyRunning.ProcessId -join ', '))."
    exit 0
}

$wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
$args = "-d $distro -u root -- /bin/bash $marker"
$process = Start-Process -FilePath $wsl -ArgumentList $args -WindowStyle Hidden -PassThru
Write-Host "Started WSL keeper (PID $($process.Id))."
