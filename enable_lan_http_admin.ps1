#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [switch]$InstallBootTask,
    [switch]$WaitForWslIp,
    [string]$WslIp
)

$ErrorActionPreference = 'Stop'
$Distro = 'BRMMedia-Ubuntu'
$TaskName = 'BRMMedia-LAN-HTTP-Forwarder'
$ScriptPath = 'D:\brmmedia\source\enable_lan_http_admin.ps1'
$RuleName = 'BRMMedia LAN HTTP'
$WslIpFile = 'D:\brmmedia\artifacts\wsl-ip.txt'

function Test-IPv4Address {
    param([string]$Address)
    $parsed = $null
    return [System.Net.IPAddress]::TryParse($Address, [ref]$parsed) -and
        $parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork
}

function Get-CurrentWslIp {
    if (Test-IPv4Address $WslIp) {
        return $WslIp
    }

    # The distro is registered to Windows user `deploy`, whereas this script
    # runs elevated as the interactive administrator. The keeper task reports
    # the deploy-owned distro IP to D: so SYSTEM/admin can consume it safely.
    $deadline = (Get-Date).AddSeconds($(if ($WaitForWslIp) { 120 } else { 0 }))
    do {
        if (Test-Path -LiteralPath $WslIpFile) {
            $candidate = (Get-Content -LiteralPath $WslIpFile -TotalCount 1 -ErrorAction SilentlyContinue).Trim()
            if (Test-IPv4Address $candidate) {
                return $candidate
            }
        }

        # Direct lookup is useful only when the calling Windows account owns
        # the distro. Avoid emitting WSL_E_DISTRO_NOT_FOUND for administrators
        # such as `sun`, which do not own the deploy user's distribution.
        if (-not $WaitForWslIp) {
            $addressText = (& "$env:WINDIR\System32\wsl.exe" -d $Distro -u brm -- hostname -I 2>$null).Trim()
            if ($LASTEXITCODE -eq 0) {
                $candidate = @($addressText -split '\s+' | Where-Object { $_ } | Select-Object -First 1)[0]
                if (Test-IPv4Address $candidate) {
                    return $candidate
                }
            }
        }

        if ((Get-Date) -ge $deadline) { break }
        Start-Sleep -Seconds 3
    } while ($true)

    throw "Unable to determine the IPv4 address for $Distro. Expected reporter file: $WslIpFile"
}

$wslIp = Get-CurrentWslIp

# Keep only the single, stable Windows LAN endpoint. Recreate the mapping so a
# changed WSL NAT address never leaves a stale listener behind.
& netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=80 | Out-Null
& netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=80 connectaddress=$wslIp connectport=80 | Out-Null

Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 80 -RemoteAddress '192.168.1.0/24' -Profile Any | Out-Null

if ($InstallBootTask) {
    # Run as SYSTEM after boot and wait for the deploy-owned keeper to publish
    # the freshly assigned WSL address before recreating portproxy.
    $action = New-ScheduledTaskAction -Execute 'PowerShell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -WaitForWslIp"
    $trigger = New-ScheduledTaskTrigger -AtStartup
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
}

Write-Host "BRMMedia LAN HTTP is forwarded: http://192.168.1.106/ -> $wslIp:80"
Write-Host "Firewall scope: 192.168.1.0/24 only"
if ($InstallBootTask) {
    Write-Host "Boot refresh task installed: $TaskName"
}
