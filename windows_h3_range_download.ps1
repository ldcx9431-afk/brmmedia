param(
    [string]$Url,

    [string]$Target,

    [Int64]$ExpectedBytes,

    [Int64]$ChunkBytes = 67108864,

    # A non-zero value is useful for a no-risk transport probe: it writes only
    # that many verified chunks and then exits. Production uses the default 0.
    [int]$MaxBatches = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http

if ([string]::IsNullOrWhiteSpace($Url) -or [string]::IsNullOrWhiteSpace($Target) -or $ExpectedBytes -le 0) {
    throw 'Url, Target, and a positive ExpectedBytes are required.'
}
if ($ChunkBytes -lt 1048576 -or $ChunkBytes -gt 268435456 -or $MaxBatches -lt 0) {
    throw 'ChunkBytes must be 1MiB–256MiB and MaxBatches cannot be negative.'
}

$targetDirectory = [System.IO.Path]::GetDirectoryName($Target)
if (-not (Test-Path -LiteralPath $targetDirectory -PathType Container)) {
    throw "Target directory does not exist: $targetDirectory"
}

function Get-VerifiedLength {
    if (-not (Test-Path -LiteralPath $Target -PathType Leaf)) {
        return [Int64]0
    }
    return [Int64](Get-Item -LiteralPath $Target).Length
}

$initialLength = Get-VerifiedLength
if ($initialLength -gt $ExpectedBytes) {
    throw "Target exceeds expected length ($initialLength > $ExpectedBytes): $Target"
}

$temporaryPath = "$Target.range-$PID.tmp"
$client = [System.Net.Http.HttpClient]::new()
$client.Timeout = [TimeSpan]::FromMinutes(15)
# ModelScope's download edge rejects the empty default .NET user agent while
# accepting the same byte range from curl.  Supply an explicit neutral agent;
# this does not change the immutable URL/range or any verification step.
$client.DefaultRequestHeaders.UserAgent.ParseAdd('curl/8.0')
$completedBatches = 0

try {
    while ($true) {
        $start = Get-VerifiedLength
        if ($start -eq $ExpectedBytes) {
            Write-Output "[OK] $Target already reached $ExpectedBytes bytes."
            break
        }
        if ($start -gt $ExpectedBytes) {
            throw "Target exceeded expected length during download ($start > $ExpectedBytes)."
        }
        $length = [Math]::Min($ChunkBytes, $ExpectedBytes - $start)
        $end = $start + $length - 1

        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
        $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $Url)
        $request.Headers.Range = [System.Net.Http.Headers.RangeHeaderValue]::new($start, $end)
        $response = $client.SendAsync($request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        try {
            if ($response.StatusCode -ne [System.Net.HttpStatusCode]::PartialContent) {
                throw "Range request did not return 206 (status=$([int]$response.StatusCode))."
            }
            $contentRange = $response.Content.Headers.ContentRange
            if ($null -eq $contentRange -or $contentRange.From -ne $start -or $contentRange.To -ne $end -or $contentRange.Length -ne $ExpectedBytes) {
                throw "Unexpected Content-Range: $contentRange (wanted bytes $start-$end/$ExpectedBytes)."
            }
            $input = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            try {
                $output = [System.IO.File]::Open($temporaryPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
                try {
                    $input.CopyTo($output)
                } finally {
                    $output.Dispose()
                }
            } finally {
                $input.Dispose()
            }
        } finally {
            $response.Dispose()
            $request.Dispose()
        }
        $received = [Int64](Get-Item -LiteralPath $temporaryPath).Length
        if ($received -ne $length) {
            throw "Rejected incomplete chunk: received=$received expected=$length range=$start-$end."
        }
        $append = [System.IO.File]::Open($Target, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try {
            $chunk = [System.IO.File]::OpenRead($temporaryPath)
            try {
                $chunk.CopyTo($append)
            } finally {
                $chunk.Dispose()
            }
        } finally {
            $append.Dispose()
        }
        Remove-Item -LiteralPath $temporaryPath -Force
        $completedBatches++
        Write-Output "[OK] Appended verified bytes $start-$end/$ExpectedBytes to $Target"
        if ($MaxBatches -gt 0 -and $completedBatches -ge $MaxBatches) {
            break
        }
    }
} finally {
    $client.Dispose()
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}
