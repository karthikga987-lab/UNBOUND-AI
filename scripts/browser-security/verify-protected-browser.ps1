[CmdletBinding()]
param(
    [string]$Root = (Join-Path $env:ProgramFiles 'UNBOUND AI'),
    [switch]$Json
)

. (Join-Path $PSScriptRoot 'protected-deployment-common.ps1')
try {
    $manifest = Test-UnboundDeployment $Root -CheckAcl
    $result = [pscustomobject]@{
        verified = $true
        deployment_id = [string]$manifest.deployment_id
        root = [IO.Path]::GetFullPath($Root)
        proxy_path = Join-Path $Root ([string]$manifest.proxy_path.Replace('/', '\'))
        proxy_sha256 = [string]$manifest.proxy_sha256
        chromium_path = Join-Path $Root ([string]$manifest.chromium_path.Replace('/', '\'))
        chromium_sha256 = [string]$manifest.chromium_sha256
        acl_verified = $true
    }
    if ($Json) { $result | ConvertTo-Json -Compress } else { $result }
    exit 0
} catch {
    $result = [pscustomobject]@{ verified = $false; error = 'protected_deployment_verification_failed' }
    if ($Json) { $result | ConvertTo-Json -Compress } else { Write-Error $result.error }
    exit 2
}
