#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('qwen38', 'qwen35')]
    [string]$Target,
    [string]$Distro = 'BRMMedia-Ubuntu',
    [string]$WslProxyScript = '/srv/brmmedia/app/llm-backend-deploy/install_qwen38_nginx_proxy.sh',
    [string]$WslOwner = 'deploy'
)

$ErrorActionPreference = 'Stop'
$taskName = 'BRMMedia-Qwen38-Llama'
$bridgeKey = Join-Path $env:ProgramData 'BRMMedia\qwen38\deploy-wsl-bridge'
function Invoke-WslViaDeployBridge([string[]]$Arguments) {
    if (-not (Test-Path $bridgeKey)) {
        throw "Local WSL bridge key is missing: $bridgeKey. Run install_qwen38_windows_admin.ps1 first."
    }
    & ssh.exe -i $bridgeKey -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "$WslOwner@127.0.0.1" wsl.exe -d $Distro -u root -- @Arguments
    if ($LASTEXITCODE -ne 0) { throw "WSL bridge command failed: $($Arguments -join ' ')" }
}
function Invoke-WslRoot([string[]]$Arguments) {
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name.Split('\\')[-1]
    if ($currentUser -ne $WslOwner) {
        Invoke-WslViaDeployBridge $Arguments
        return
    }
    $wsl = "$env:WINDIR\System32\wsl.exe"
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        $output = & $wsl -d $Distro -u root -- @Arguments 2>&1
        $exitCode = $LASTEXITCODE
        if ($output) { $output | Write-Host }
        if ($exitCode -eq 0) { return }
        $text = ($output | Out-String)
        # A running WSL session can momentarily hold the VHDX while the Windows
        # CLI opens a second client. Retrying is safe; never call wsl --shutdown
        # because that would interrupt the media queue and model download.
        if ($text -match 'HCS_ERROR_SHARING_VIOLATION|无法将磁盘|cannot attach.*vhdx' -and $attempt -lt 10) {
            Start-Sleep -Seconds 3
            continue
        }
        throw "WSL command failed: $($Arguments -join ' ')"
    }
}
function Wait-Http([string]$Url, [string]$Model) {
    foreach ($ignored in 1..180) {
        try {
            $body = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri $Url).Content
            if ($body -match [regex]::Escape($Model)) { return }
        } catch {}
        Start-Sleep -Seconds 2
    }
    throw "Health check did not become ready: $Url"
}

if ($Target -eq 'qwen38') {
    Invoke-WslRoot @('systemctl', 'stop', 'qwen-vllm')
    & schtasks.exe /Run /TN $taskName | Out-Host
    try {
        Wait-Http 'http://127.0.0.1:8001/v1/models' 'qwen38-27b-ud-q4-xl'
        Invoke-WslRoot @($WslProxyScript, 'qwen38')
        Invoke-WslRoot @('systemctl', 'disable', 'qwen-vllm')
        Write-Host '[OK] Qwen3.8 is now the active /qwen/v1 model.'
    } catch {
        & schtasks.exe /End /TN $taskName 2>$null
        Invoke-WslRoot @('systemctl', 'start', 'qwen-vllm')
        throw
    }
} else {
    & schtasks.exe /End /TN $taskName 2>$null
    Invoke-WslRoot @('systemctl', 'enable', '--now', 'qwen-vllm')
    try {
        $ready = $false
        foreach ($ignored in 1..120) {
            try {
                Invoke-WslRoot @('curl', '-fsS', '--max-time', '5', 'http://127.0.0.1:8000/v1/models')
                $ready = $true
                break
            } catch {
                Start-Sleep -Seconds 2
            }
        }
        if (-not $ready) { throw 'Qwen3.5 fallback health check did not become ready.' }
        Invoke-WslRoot @($WslProxyScript, 'qwen35')
        Write-Host '[OK] Qwen3.5 is now the active /qwen/v1 fallback model.'
    } catch {
        & schtasks.exe /Run /TN $taskName 2>$null
        throw
    }
}
