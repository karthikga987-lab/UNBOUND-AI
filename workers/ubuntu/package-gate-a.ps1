[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Wheelhouse,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$lock = Join-Path $PSScriptRoot 'requirements-cp312-linux-x86_64.lock'
$wheelRoot = (Resolve-Path -LiteralPath $Wheelhouse).Path
$outRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
if (-not (Test-Path -LiteralPath $wheelRoot -PathType Container)) { throw 'Wheelhouse is missing.' }
if (Test-Path -LiteralPath $outRoot) { throw 'Output directory already exists. Choose a new destination.' }
$null = New-Item -ItemType Directory -Path $outRoot
$stage = Join-Path $outRoot 'unbound-gate-a'
$null = New-Item -ItemType Directory -Path $stage
try {
    $sourceFiles = @(
        'backend/app/egress_proxy.py',
        'backend/app/browser/worker_protocol.py',
        'backend/app/browser/worker_service.py',
        'backend/app/browser/worker_ipc.py',
        'backend/app/browser/worker_fetch.py',
        'backend/app/browser/worker_renderer.py',
        'workers/ubuntu/check_renderer_boundary.sh',
        'workers/ubuntu/check_disconnected_renderer.py',
        'workers/ubuntu/unbound-renderer.service',
        'workers/ubuntu/unbound-browser-worker.service',
        'workers/ubuntu/host-only-review.nft',
        'workers/ubuntu/requirements-cp312-linux-x86_64.lock'
    )
    foreach ($relative in $sourceFiles) {
        $source = Join-Path $repo $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing package input: $relative" }
        $target = Join-Path $stage $relative
        $null = New-Item -ItemType Directory -Force -Path (Split-Path $target)
        Copy-Item -LiteralPath $source -Destination $target
    }
    # Use a deliberately empty browser package initializer so the offline worker
    # does not import the full Windows application during package initialization.
    foreach ($relative in @('backend/__init__.py','backend/app/__init__.py','backend/app/browser/__init__.py','workers/__init__.py','workers/ubuntu/__init__.py')) {
        $target = Join-Path $stage $relative
        $null = New-Item -ItemType Directory -Force -Path (Split-Path $target)
        [System.IO.File]::WriteAllText($target, '', [System.Text.UTF8Encoding]::new($false))
    }
    $wheelTarget = Join-Path $stage 'wheelhouse'
    $null = New-Item -ItemType Directory -Path $wheelTarget
    foreach ($line in Get-Content -LiteralPath $lock) {
        if ($line -match '^\s*#' -or [string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -notmatch '^([A-Za-z0-9_.-]+)==([^ ]+) --hash=sha256:([0-9a-f]{64})$') { throw "Unexpected lock entry: $line" }
        $name = $Matches[1].Replace('-', '_').ToLowerInvariant()
        $version = $Matches[2]
        $expected = $Matches[3]
        $wheel = Get-ChildItem -LiteralPath $wheelRoot -Filter '*.whl' | Where-Object {
            $_.Name.ToLowerInvariant().StartsWith("$name-$version-")
        }
        if (@($wheel).Count -ne 1) { throw "Expected exactly one locked wheel for $name $version." }
        $actual = (Get-FileHash -LiteralPath $wheel.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) { throw "Wheel hash mismatch for $($wheel.Name)." }
        Copy-Item -LiteralPath $wheel.FullName -Destination $wheelTarget
    }
    $hashes = Get-ChildItem -LiteralPath $stage -File -Recurse | Sort-Object FullName | ForEach-Object {
        [PSCustomObject]@{ path = $_.FullName.Substring($stage.Length + 1).Replace('\','/'); sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
    }
    $hashes | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $stage 'SHA256SUMS.json') -Encoding utf8
    $archive = Join-Path $outRoot 'unbound-gate-a.zip'
    Compress-Archive -LiteralPath $stage -DestinationPath $archive -CompressionLevel Optimal
    Get-FileHash -LiteralPath $archive -Algorithm SHA256 | Select-Object Path, Hash | Format-List
    Get-Content -LiteralPath (Join-Path $stage 'SHA256SUMS.json')
} catch {
    throw
}
