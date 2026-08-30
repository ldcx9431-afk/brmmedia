[CmdletBinding()]
param(
    [ValidateRange(60000, 2147483647)]
    [int]$VmIdleTimeoutMs = 86400000
)

$ErrorActionPreference = 'Stop'
$configPath = Join-Path $env:USERPROFILE '.wslconfig'
$backupPath = "$configPath.brmmedia.bak"

if (Test-Path $configPath) {
    Copy-Item -LiteralPath $configPath -Destination $backupPath -Force
    $content = Get-Content -LiteralPath $configPath -Raw
} else {
    $content = ''
}

$line = "vmIdleTimeout=$VmIdleTimeoutMs"
if ($content -match '(?im)^\s*\[wsl2\]\s*$') {
    $content = [regex]::Replace(
        $content,
        '(?ims)(^\s*\[wsl2\]\s*$)(.*?)(?=^\s*\[|\z)',
        {
            param($m)
            $body = [regex]::Replace($m.Groups[2].Value, '(?im)^\s*vmIdleTimeout\s*=.*(?:\r?\n|\z)', '')
            "$($m.Groups[1].Value)`r`n$line`r`n$body"
        }
    )
} else {
    if ($content.Length -gt 0 -and -not $content.EndsWith("`n")) { $content += "`r`n" }
    $content += "[wsl2]`r`n$line`r`n"
}

[System.IO.File]::WriteAllText($configPath, $content, [System.Text.UTF8Encoding]::new($false))
Write-Host "Configured $configPath with $line"
Write-Host 'Restarting the WSL VM so the setting takes effect...'
wsl --shutdown
