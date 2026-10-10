#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$RuntimeRoot = 'E:\BRMMedia\qwen38-llama',
    [string]$ModelSourceRoot = 'D:\model',
    [string]$LlamaBuild = 'b10236',
    [string]$ModelRevision = 'f1bfb127c64f7072bdd2cad55f258b9c8b2910fe',
    [string]$WslOwner = 'deploy'
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $PSCommandPath
$modelName = 'Qwen3.8-27B-UD-Q4_K_XL.gguf'
$modelSource = Join-Path $ModelSourceRoot $modelName
$modelRuntime = Join-Path $RuntimeRoot "models\$modelName"
$expectedModelBytes = [Int64]17923394624
$expectedModelSha256 = 'bee238bbeb3dc0a34bde4d0dedbaee1f98c009e8bb4226f03070054c12fb1372'
$archiveRoot = Join-Path $RuntimeRoot 'archives'
$binRoot = Join-Path $RuntimeRoot 'bin'
$manifest = Join-Path $RuntimeRoot 'MANIFEST.sha256'
$bridgeRoot = Join-Path $env:ProgramData 'BRMMedia\qwen38'
$bridgeKey = Join-Path $bridgeRoot 'deploy-wsl-bridge'
$bridgeAuthorizedKeys = "C:\Users\$WslOwner\.ssh\authorized_keys"
New-Item -ItemType Directory -Force -Path $archiveRoot, $binRoot, (Split-Path $modelRuntime) | Out-Null

function Install-LocalWslBridge {
    New-Item -ItemType Directory -Force -Path $bridgeRoot | Out-Null
    if (-not (Test-Path $bridgeKey)) {
        & ssh-keygen.exe -q -t ed25519 -N '""' -f $bridgeKey -C 'BRMMedia Qwen38 local WSL bridge'
        if ($LASTEXITCODE -ne 0) { throw 'Unable to create the local WSL bridge key.' }
    }
    $publicKey = (Get-Content -Raw "$bridgeKey.pub").Trim()
    New-Item -ItemType Directory -Force -Path (Split-Path $bridgeAuthorizedKeys) | Out-Null
    if (-not (Test-Path $bridgeAuthorizedKeys) -or -not (Select-String -LiteralPath $bridgeAuthorizedKeys -SimpleMatch $publicKey -Quiet)) {
        Add-Content -LiteralPath $bridgeAuthorizedKeys -Value $publicKey -Encoding ascii
    }
    & icacls.exe $bridgeRoot /inheritance:r /grant 'Administrators:(OI)(CI)F' /grant 'SYSTEM:(OI)(CI)F' | Out-Null
    & icacls.exe $bridgeAuthorizedKeys /inheritance:r /grant "${WslOwner}:F" /grant 'SYSTEM:F' | Out-Null
}

function Get-ResumableFile([string]$Url, [string]$Destination) {
    if (Test-Path $Destination) { return }
    Write-Host "Downloading $Url"
    & curl.exe -L --fail --retry 5 --retry-delay 3 --continue-at - --output $Destination $Url
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $Url" }
}

$binaryZip = Join-Path $archiveRoot "llama-$LlamaBuild-win-cuda13.zip"
$runtimeZip = Join-Path $archiveRoot "cudart-llama-$LlamaBuild-win-cuda13.zip"
$base = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaBuild"
Get-ResumableFile "$base/llama-$LlamaBuild-bin-win-cuda-13.3-x64.zip" $binaryZip
Get-ResumableFile "$base/cudart-llama-bin-win-cuda-13.3-x64.zip" $runtimeZip

if (-not (Test-Path (Join-Path $binRoot 'llama-server.exe'))) {
    Expand-Archive -Path $binaryZip -DestinationPath $binRoot -Force
    Expand-Archive -Path $runtimeZip -DestinationPath $binRoot -Force
    $server = Get-ChildItem -Path $binRoot -Filter llama-server.exe -Recurse | Select-Object -First 1
    if (-not $server) { throw 'llama-server.exe was not found in the official archive.' }
    Get-ChildItem -Path $server.DirectoryName -File | Copy-Item -Destination $binRoot -Force
}

if (-not (Test-Path $modelSource) -or (Get-Item $modelSource).Length -ne $expectedModelBytes) {
    $url = "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/$ModelRevision/$modelName?download=true"
    $attempt = 0
    while (-not (Test-Path $modelSource) -or (Get-Item $modelSource).Length -lt $expectedModelBytes) {
        $attempt++
        Write-Host "Resuming Qwen3.8 model download (attempt $attempt)"
        & curl.exe -L --fail --retry 8 --retry-delay 5 --continue-at - --output $modelSource $url
        Start-Sleep -Seconds 1
    }
    if ((Get-Item $modelSource).Length -ne $expectedModelBytes) { throw 'Qwen3.8 source model size is invalid.' }
}
if (-not (Test-Path $modelRuntime)) {
    Write-Host "Copying verified model source to NVMe runtime: $modelRuntime"
    Copy-Item -Path $modelSource -Destination $modelRuntime
}

$sourceHash = (Get-FileHash -Algorithm SHA256 $modelSource).Hash.ToLowerInvariant()
if ($sourceHash -ne $expectedModelSha256) { throw "Qwen3.8 source SHA-256 mismatch: $sourceHash" }
$runtimeHash = (Get-FileHash -Algorithm SHA256 $modelRuntime).Hash.ToLowerInvariant()
if ($sourceHash -ne $runtimeHash) { throw 'Model source and NVMe runtime SHA-256 differ.' }
$runScript = Join-Path $RuntimeRoot 'run_qwen38_llama.ps1'
Copy-Item -Path (Join-Path $scriptRoot 'run_qwen38_llama.ps1') -Destination $runScript -Force
@(
    "llama_build=$LlamaBuild",
    "model_repo=unsloth/Qwen3.8-27B-GGUF",
    "model_revision=$ModelRevision",
    "model_file=$modelName",
    "model_sha256=$runtimeHash",
    "runtime_path=$modelRuntime"
) | Set-Content -Path $manifest -Encoding ascii

$taskName = 'BRMMedia-Qwen38-Llama'
$taskRun = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runScript`""
& schtasks.exe /Create /TN $taskName /TR $taskRun /SC ONSTART /RU SYSTEM /RL HIGHEST /F | Out-Host
New-NetFirewallRule -DisplayName 'BRMMedia Qwen38 WSL bridge' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8001 -RemoteAddress '172.16.0.0/12' -ErrorAction SilentlyContinue | Out-Null
Install-LocalWslBridge
Write-Host "[OK] Qwen3.8 runtime prepared. It is not started by this installer. Use switch_qwen_active_windows_admin.ps1 qwen38 after candidate validation."
