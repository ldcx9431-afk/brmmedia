[CmdletBinding()]
param(
    [string]$RuntimeRoot = 'E:\BRMMedia\qwen38-llama'
)

$ErrorActionPreference = 'Stop'
$server = Join-Path $RuntimeRoot 'bin\llama-server.exe'
$model = Join-Path $RuntimeRoot 'models\Qwen3.8-27B-UD-Q4_K_XL.gguf'
$logDir = Join-Path $RuntimeRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
if (-not (Test-Path $server)) { throw "llama-server.exe is missing: $server" }
if (-not (Test-Path $model)) { throw "Qwen3.8 model is missing: $model" }

$env:CUDA_VISIBLE_DEVICES = '1,2'
$env:CUDA_DEVICE_ORDER = 'PCI_BUS_ID'
$env:GGML_CUDA_ENABLE_UNIFIED_MEMORY = '0'
$args = @(
    '--model', $model,
    '--alias', 'qwen38-27b-ud-q4-xl',
    '--host', '0.0.0.0',
    '--port', '8001',
    '--ctx-size', '4096',
    '--parallel', '1',
    '--gpu-layers', 'all',
    '--split-mode', 'layer',
    '--tensor-split', '1,1',
    '--flash-attn', 'auto',
    '--log-colors', 'off',
    '--metrics'
)

# Scheduled Tasks do not provide a restart-on-crash policy.  Keep the native
# inference process supervised without exposing any additional network port.
while ($true) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $log = Join-Path $logDir "llama-server-$stamp.log"
    & $server @args *>> $log
    Start-Sleep -Seconds 15
}
