[CmdletBinding()]
param(
    [string]$ModelSourceRoot = 'D:\model',
    [string]$ModelRevision = 'f1bfb127c64f7072bdd2cad55f258b9c8b2910fe'
)

$ErrorActionPreference = 'Stop'
$name = 'Qwen3.8-27B-UD-Q4_K_XL.gguf'
$destination = Join-Path $ModelSourceRoot $name
$log = Join-Path $env:USERPROFILE 'qwen38-udq4-download.log'
New-Item -ItemType Directory -Force -Path $ModelSourceRoot | Out-Null
if (Test-Path $destination) {
    Write-Host "[OK] Model source already exists: $destination"
    exit 0
}
$url = "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/$ModelRevision/$name?download=true"
Write-Host "Downloading with resume support to $destination"
& curl.exe -L --fail --retry 8 --retry-delay 5 --continue-at - --output $destination $url *>> $log
if ($LASTEXITCODE -ne 0) { throw "Model download failed; see $log" }
Write-Host "[OK] Download complete: $destination"
