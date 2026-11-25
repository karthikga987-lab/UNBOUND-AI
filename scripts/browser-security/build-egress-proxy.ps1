param(
    [string]$Python = ".\.venv-wake\Scripts\python.exe"
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pythonPath = if ([System.IO.Path]::IsPathRooted($Python)) {
    (Resolve-Path -LiteralPath $Python).Path
} else {
    (Resolve-Path -LiteralPath (Join-Path $repoRoot $Python)).Path
}
$entrypoint = Join-Path $repoRoot 'backend\app\egress_proxy_entrypoint.py'
$distDirectory = Join-Path $repoRoot 'backend\app\browser\bin'
$outputExecutable = Join-Path $distDirectory 'unbound-egress-proxy\unbound-egress-proxy.exe'

if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
    throw 'Proxy source entry point is missing.'
}
if (Test-Path -LiteralPath $outputExecutable) {
    throw "Refusing to overwrite existing proxy executable: $outputExecutable"
}
if (-not (Test-Path -LiteralPath $distDirectory)) {
    New-Item -ItemType Directory -Path $distDirectory | Out-Null
}

$buildId = [guid]::NewGuid().ToString('N')
$buildDirectory = Join-Path $env:TEMP "unbound-proxy-build-$buildId"
$workDirectory = Join-Path $buildDirectory 'work'
$specDirectory = Join-Path $buildDirectory 'spec'
New-Item -ItemType Directory -Path $buildDirectory | Out-Null
New-Item -ItemType Directory -Path $workDirectory, $specDirectory | Out-Null

Push-Location $repoRoot
try {
    & $pythonPath -m PyInstaller --noconfirm --clean --onedir --console `
        --name unbound-egress-proxy `
        --collect-submodules dns `
        --distpath $distDirectory `
        --workpath $workDirectory `
        --specpath $specDirectory `
        --paths $repoRoot `
        $entrypoint
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $outputExecutable -PathType Leaf)) {
    throw 'Build completed without creating the expected proxy executable.'
}
Get-Item -LiteralPath $outputExecutable | Select-Object FullName, Length, LastWriteTimeUtc
Get-FileHash -LiteralPath $outputExecutable -Algorithm SHA256
Write-Output "Build intermediates preserved at: $buildDirectory"
