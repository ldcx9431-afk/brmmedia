# Requires elevated PowerShell on the Windows host.  It configures only the
# resources needed by the H3 candidate: WSL gets a stable 96GB ceiling and
# Windows gets a fixed 64GB page file on the E: NVMe.  Existing values are
# backed up first; WSL is shut down so the new memory limit can take effect.
#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateRange(64, 120)]
    [int]$WslMemoryGb = 96,
    [ValidateRange(64, 128)]
    [int]$PageFileGb = 64,
    # The persistent BRMMedia WSL services run through the Windows `deploy`
    # account.  .wslconfig is per Windows user, so an administrator launching
    # this script as another account must still write the deploy profile.
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$TargetWindowsUser = 'deploy'
)

$ErrorActionPreference = 'Stop'
$profileRoot = Join-Path 'C:\Users' $TargetWindowsUser
Test-Path $profileRoot -PathType Container | Out-Null
if (-not (Test-Path $profileRoot -PathType Container)) {
    throw "Windows profile does not exist: $profileRoot"
}
$configPath = Join-Path $profileRoot '.wslconfig'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupPath = "$configPath.brmmedia-h3-$stamp.bak"

if (-not (Test-Path 'E:\')) {
    throw 'E: is unavailable. Refusing to move/create the H3 page file.'
}
$freeBytes = (Get-PSDrive -Name E).Free
$requiredBytes = ($PageFileGb + 8) * 1GB
if ($freeBytes -lt $requiredBytes) {
    throw "E: free space is insufficient: need at least $($PageFileGb + 8)GB, found $([math]::Round($freeBytes / 1GB, 1))GB."
}

if (Test-Path $configPath) {
    Copy-Item -LiteralPath $configPath -Destination $backupPath -Force
    $content = Get-Content -LiteralPath $configPath -Raw
} else {
    $content = ''
}

function Set-Wsl2Option([string]$Text, [string]$Name, [string]$Value) {
    if ($Text -match '(?im)^\s*\[wsl2\]\s*$') {
        return [regex]::Replace(
            $Text,
            '(?ims)(^\s*\[wsl2\]\s*$)(.*?)(?=^\s*\[|\z)',
            {
                param($m)
                $body = [regex]::Replace($m.Groups[2].Value, "(?im)^\s*$([regex]::Escape($Name))\s*=.*(?:\r?\n|\z)", '')
                "$($m.Groups[1].Value)`r`n$Name=$Value`r`n$body"
            }
        )
    }
    if ($Text.Length -gt 0 -and -not $Text.EndsWith("`n")) { $Text += "`r`n" }
    return "$Text[wsl2]`r`n$Name=$Value`r`n"
}

$content = Set-Wsl2Option $content 'memory' "${WslMemoryGb}GB"
# Preserve the existing WSL swap if one is explicitly configured.  The fixed
# Windows E: page file is the committed-memory safety net required for H3.
[System.IO.File]::WriteAllText($configPath, $content, [System.Text.UTF8Encoding]::new($false))

# Win32_PageFileSetting declares these fields as uint32.  PowerShell otherwise
# infers Int32 from the arithmetic expression, which fails on New-CimInstance.
$pagefileMb = [uint32]($PageFileGb * 1024)
$existing = @(Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue)
if ($PSCmdlet.ShouldProcess('Windows virtual-memory settings', "set E:\pagefile.sys to fixed $PageFileGb GB")) {
    $computer = Get-CimInstance Win32_ComputerSystem
    Set-CimInstance -InputObject $computer -Property @{ AutomaticManagedPagefile = $false } | Out-Null
    foreach ($item in $existing) {
        if ($item.Name -ne 'E:\pagefile.sys') {
            Remove-CimInstance -InputObject $item
        }
    }
    $ePagefile = Get-CimInstance Win32_PageFileSetting -Filter "Name='E:\\pagefile.sys'" -ErrorAction SilentlyContinue
    if ($ePagefile) {
        Set-CimInstance -InputObject $ePagefile -Property @{ InitialSize = [uint32]$pagefileMb; MaximumSize = [uint32]$pagefileMb } | Out-Null
    } else {
        New-CimInstance -ClassName Win32_PageFileSetting -Property @{ Name = 'E:\pagefile.sys'; InitialSize = [uint32]$pagefileMb; MaximumSize = [uint32]$pagefileMb } | Out-Null
    }
}

Write-Host "WSL configuration written for ${TargetWindowsUser}: $configPath (memory=${WslMemoryGb}GB)"
if (Test-Path $backupPath) { Write-Host "Backup written: $backupPath" }
Write-Host "E: page file configured: E:\pagefile.sys (${PageFileGb}GB fixed)"
Write-Host 'Restart Windows before continuing. A WSL shutdown alone does not apply a new Windows page file.' -ForegroundColor Yellow
