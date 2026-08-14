[CmdletBinding()]
param(
    [string]$ModelRoot = 'D:\model\IndexTTS-2.5',
    [string]$Revision = 'c39ce5ba981572cb187443877ff559dfb246ce63',
    [string[]]$OnlyFile
)

$ErrorActionPreference = 'Stop'

$items = @(
    @('codec.pth', 607290935, 'd15cbed16a40f478438c961fb043f68dfa6353bf56c966761315db3433e9722c'),
    @('gpt.pth', 3259599833, '43a8f4c30eccdf201958d3b9713511482c19d56dc20b0b1c4ee1e6b080b19d85'),
    @('qwen0.6bemo4-merge/model.safetensors', 1192135096, '11293257a8df593c154a8ecd5fc039f3076de35411e35f06d41b471e136f6641'),
    @('qwen0.6bemo4-merge/tokenizer.json', 11422654, 'aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4'),
    @('s2mel.pth', 414908601, '9b1b0003fc189c94cc349758d7ebc25f903b7eb2de4602879959cc64ce816456')
)

$logSuffix = if ($OnlyFile) { '-' + (($OnlyFile -join '_') -replace '[^A-Za-z0-9._-]', '_') } else { '' }
$logFile = Join-Path $ModelRoot "download-indextts25$logSuffix.log"
New-Item -ItemType Directory -Force -Path $ModelRoot | Out-Null

function Write-DownloadLog([string]$Message) {
    "$(Get-Date -Format s) $Message" | Out-File -LiteralPath $logFile -Append -Encoding ascii
}

function Test-VerifiedFile([string]$Path, [Int64]$ExpectedSize, [string]$Sha256) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    if ((Get-Item -LiteralPath $Path).Length -ne $ExpectedSize) { return $false }
    return ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -eq $Sha256)
}

foreach ($item in $items) {
    $relativePath = $item[0]
    if ($OnlyFile -and $relativePath -notin $OnlyFile) { continue }
    $expectedSize = [Int64]$item[1]
    $expectedSha = $item[2]
    $destination = Join-Path $ModelRoot ($relativePath -replace '/', '\\')
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null

    if (Test-VerifiedFile $destination $expectedSize $expectedSha) {
        Write-DownloadLog "OK cached $relativePath"
        continue
    }

    if (Test-Path -LiteralPath $destination) {
        $offset = [Int64](Get-Item -LiteralPath $destination).Length
        if ($offset -gt $expectedSize) {
            Remove-Item -LiteralPath $destination -Force
            $offset = 0
        }
    } else {
        $offset = 0
    }

    Write-DownloadLog "START $relativePath $offset/$expectedSize"
    $uri = "https://huggingface.co/IndexTeam/IndexTTS-2.5/resolve/$Revision/$relativePath"
    # The server confirms 2 MiB HTTP ranges reliably.  This halves request
    # setup overhead while retaining per-range retries and final SHA-256.
    $chunkSize = [Int64](2MB)
    $stream = [IO.File]::Open($destination, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try {
        $offset = [Int64]$stream.Length
        while ($offset -lt $expectedSize) {
            $end = [Math]::Min($offset + $chunkSize - 1, $expectedSize - 1)
            $completed = $false
            for ($attempt = 1; $attempt -le 25 -and -not $completed; $attempt++) {
                try {
                    $request = [Net.HttpWebRequest]::Create($uri)
                    $request.Timeout = 180000
                    $request.ReadWriteTimeout = 180000
                    $request.AddRange($offset, $end)
                    $response = $request.GetResponse()
                    try {
                        $responseStream = $response.GetResponseStream()
                        try {
                            $required = [int]($end - $offset + 1)
                            $buffer = New-Object byte[] $required
                            $readOffset = 0
                            while ($readOffset -lt $required) {
                                $read = $responseStream.Read($buffer, $readOffset, $required - $readOffset)
                                if ($read -le 0) { break }
                                $readOffset += $read
                            }
                            if ($readOffset -ne $required) { throw "incomplete range $readOffset/$required" }
                            $stream.Position = $offset
                            $stream.Write($buffer, 0, $required)
                            $stream.Flush()
                            $offset += $required
                            $completed = $true
                            if (($offset % (64MB)) -eq 0 -or $offset -eq $expectedSize) {
                                Write-DownloadLog "PROGRESS $relativePath $offset/$expectedSize"
                            }
                        } finally {
                            $responseStream.Dispose()
                        }
                    } finally {
                        $response.Dispose()
                    }
                } catch {
                    Write-DownloadLog "RETRY $relativePath at $offset attempt ${attempt}: $($_.Exception.Message)"
                    if ($attempt -eq 25) {
                        throw "Download failed for $relativePath at $offset after 25 attempts: $($_.Exception.Message)"
                    }
                    Start-Sleep -Seconds ([Math]::Min(30, $attempt * 2))
                }
            }
        }
    } finally {
        $stream.Dispose()
    }

    if (-not (Test-VerifiedFile $destination $expectedSize $expectedSha)) {
        throw "SHA-256 verification failed for $relativePath"
    }
    Write-DownloadLog "DONE $relativePath"
}

Write-DownloadLog 'COMPLETE verified IndexTTS-2.5 core LFS weights'
