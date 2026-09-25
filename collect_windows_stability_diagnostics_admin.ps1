#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [ValidateRange(1, 168)]
    [int]$Hours = 6,

    [string]$OutputRoot = 'D:\brmmedia\artifacts\stability-diagnostics'
)

$ErrorActionPreference = 'Stop'
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$outputDir = Join-Path $OutputRoot $timestamp
$since = (Get-Date).AddHours(-$Hours)
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

function Save-Text {
    param([string]$Name, [scriptblock]$Command)
    try {
        & $Command | Out-File -FilePath (Join-Path $outputDir $Name) -Encoding utf8 -Width 240
    } catch {
        "ERROR: $($_.Exception.Message)" | Out-File -FilePath (Join-Path $outputDir $Name) -Encoding utf8
    }
}

Save-Text 'summary.txt' {
    "Collected: $(Get-Date -Format o)"
    "Window start: $($since.ToString('o'))"
    "Computer: $env:COMPUTERNAME"
    "User: $env:USERDOMAIN\$env:USERNAME"
    Get-CimInstance Win32_OperatingSystem |
        Select-Object Caption, Version, BuildNumber, LastBootUpTime, TotalVisibleMemorySize, FreePhysicalMemory |
        Format-List
}

Save-Text 'wsl-status.txt' {
    wsl --status
    wsl --version
    wsl --list --verbose
}

Save-Text 'gpu.txt' {
    Get-CimInstance Win32_VideoController |
        Select-Object Name, DriverVersion, AdapterRAM, CurrentHorizontalResolution, CurrentVerticalResolution |
        Format-Table -AutoSize
    nvidia-smi
}

Save-Text 'network-and-services.txt' {
    Get-NetAdapter | Select-Object Name, Status, LinkSpeed, MacAddress, InterfaceDescription | Format-Table -AutoSize
    Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias, IPAddress, PrefixLength | Format-Table -AutoSize
    Get-Service sshd, LxssManager, vmcompute -ErrorAction SilentlyContinue | Format-Table -AutoSize
    Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in 22, 80 } |
        Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table -AutoSize
}

Save-Text 'brmmedia-tasks.txt' {
    $tasks = Get-ScheduledTask -TaskName 'BRMMedia-*' -ErrorAction SilentlyContinue
    $tasks |
        Select-Object TaskName, State, TaskPath |
        Format-Table -AutoSize
    $tasks | ForEach-Object {
        Get-ScheduledTaskInfo -TaskName $_.TaskName -TaskPath $_.TaskPath -ErrorAction SilentlyContinue
    } |
        Format-List
}

$providerPattern = 'Kernel-Power|EventLog|WHEA|Lxss|Hyper-V|HyperV|Display|nvlddmkm|NVIDIA|Tcpip|Ndis'
$events = Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = $since } |
    Where-Object {
        $_.Id -in 41, 6008, 1001, 117, 4101, 129, 153, 157 -or
        $_.ProviderName -match $providerPattern
    } |
    Select-Object TimeCreated, ProviderName, Id, LevelDisplayName, Message

$events | Export-Csv -NoTypeInformation -Encoding utf8 -Path (Join-Path $outputDir 'system-events.csv')
$events | Format-List | Out-File -FilePath (Join-Path $outputDir 'system-events.txt') -Encoding utf8 -Width 240

Write-Host "Diagnostics collected: $outputDir" -ForegroundColor Green
Write-Host 'Share system-events.txt and summary.txt after the next disconnect.' -ForegroundColor Yellow
