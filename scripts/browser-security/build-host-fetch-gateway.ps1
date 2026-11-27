param(
    [string]$Python = '.\.venv-wake\Scripts\python.exe',
    [string]$OutputRoot = (Join-Path $env:TEMP ('unbound-gateway-stage-' + [guid]::NewGuid().ToString('N')))
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$pythonPath = if ([IO.Path]::IsPathRooted($Python)) {
    (Resolve-Path -LiteralPath $Python).Path
} else {
    (Resolve-Path -LiteralPath (Join-Path $repoRoot $Python)).Path
}
$entrypoint = Join-Path $repoRoot 'backend\app\browser\host_fetch_gateway_entrypoint.py'
$output = [IO.Path]::GetFullPath($OutputRoot)
if (Test-Path -LiteralPath $output) { throw 'Output directory already exists; refusing to overwrite it.' }
if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) { throw 'Gateway entrypoint is missing.' }
New-Item -ItemType Directory -Path $output | Out-Null

Push-Location $repoRoot
try {
    & $pythonPath -m PyInstaller --noconfirm --clean --onedir --console `
        --name unbound-fetch-gateway `
        --distpath (Join-Path $output 'dist') `
        --workpath (Join-Path $output 'work') `
        --specpath (Join-Path $output 'spec') `
        --paths $repoRoot `
        $entrypoint
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }
} finally {
    Pop-Location
}

$binary = Join-Path $output 'dist\unbound-fetch-gateway\unbound-fetch-gateway.exe'
if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) { throw 'Gateway binary was not produced.' }
$hash = (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash
[pscustomobject]@{ Executable = $binary; SHA256 = $hash; Stage = $output }
