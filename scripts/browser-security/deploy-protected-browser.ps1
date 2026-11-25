[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$PlaywrightBrowsersPath = $env:PLAYWRIGHT_BROWSERS_PATH
)

. (Join-Path $PSScriptRoot 'protected-deployment-common.ps1')

$repository = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$proxySource = Join-Path $repository 'backend\app\browser\bin\unbound-egress-proxy'
$proxyExecutable = Join-Path $proxySource 'unbound-egress-proxy.exe'
if (-not (Test-Path -LiteralPath $proxyExecutable -PathType Leaf)) { throw 'Build the standalone proxy before deployment.' }
$proxySource = (Resolve-Path -LiteralPath $proxySource).Path
$proxyExecutable = (Resolve-Path -LiteralPath $proxyExecutable).Path
Assert-UnboundNoReparsePoint $proxySource
Assert-UnboundNoReparsePoint $proxyExecutable
$browserCache = if ($PlaywrightBrowsersPath) { (Resolve-Path -LiteralPath $PlaywrightBrowsersPath).Path } else { Join-Path $repository '.playwright-browsers' }
if (-not (Test-Path -LiteralPath $browserCache -PathType Container)) { throw 'Playwright browser cache is missing.' }
$browserCache = (Resolve-Path -LiteralPath $browserCache).Path
Assert-UnboundNoReparsePoint $browserCache
$chromeCandidates = @(Get-ChildItem -LiteralPath $browserCache -Directory -Filter 'chromium-*' | ForEach-Object {
    Join-Path $_.FullName 'chrome-win64\chrome.exe'
} | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
if ($chromeCandidates.Count -ne 1) { throw 'Expected exactly one installed Playwright Chromium executable.' }
$chromeSource = $chromeCandidates[0]
$chromeSourceRoot = Split-Path (Split-Path $chromeSource -Parent) -Parent
$chromeSource = (Resolve-Path -LiteralPath $chromeSource).Path
$chromeSourceRoot = (Resolve-Path -LiteralPath $chromeSourceRoot).Path
Assert-UnboundNoReparsePoint $chromeSourceRoot
Assert-UnboundNoReparsePoint $chromeSource
$chromiumFolder = Split-Path $chromeSourceRoot -Leaf
$chromiumVersion = $chromiumFolder -replace '^chromium-', ''
$destination = $script:UnboundDeploymentRoot
if (Test-Path -LiteralPath $destination) { throw "Refusing to overwrite occupied protected deployment root: $destination" }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $WhatIfPreference -and -not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Protected deployment requires an elevated PowerShell session.'
}

$stage = Join-Path $env:ProgramFiles ('.UNBOUND AI.stage-' + [guid]::NewGuid().ToString('N'))
$deploymentId = [guid]::NewGuid().ToString('D')
if (-not $PSCmdlet.ShouldProcess($destination, 'Deploy protected Chromium and standalone proxy')) {
    Write-Output "Validated source inputs. Would stage deployment $deploymentId at $stage and publish to $destination."
    return
}

New-Item -ItemType Directory -Path $stage | Out-Null
$published = $false
try {
    # Protect the empty stage before any payload is copied into it.
    Set-UnboundProtectedAcl $stage
    $proxyTarget = Join-Path $stage 'browser\proxy'
    $cacheTarget = Join-Path $stage 'playwright-browsers'
    New-Item -ItemType Directory -Path (Split-Path $proxyTarget -Parent), $cacheTarget | Out-Null
    Copy-Item -LiteralPath $proxySource -Destination $proxyTarget -Recurse
    Copy-Item -LiteralPath $chromeSourceRoot -Destination $cacheTarget -Recurse

    $proxyRelative = 'browser/proxy/unbound-egress-proxy.exe'
    $chromeRelative = "playwright-browsers/$chromiumFolder/chrome-win64/chrome.exe"
    $inventory = @(Get-UnboundInventory $stage)
    if ($inventory.Count -eq 0) { throw 'Staged browser deployment is empty.' }
    $manifest = [ordered]@{
        schema_version = 1
        application = 'UNBOUND-AI'
        deployment_id = $deploymentId
        created_utc = [DateTime]::UtcNow.ToString('O')
        proxy_path = $proxyRelative
        proxy_sha256 = Get-UnboundFileHash (Join-Path $stage ($proxyRelative.Replace('/', '\')))
        chromium_path = $chromeRelative
        chromium_version = $chromiumVersion
        chromium_sha256 = Get-UnboundFileHash (Join-Path $stage ($chromeRelative.Replace('/', '\')))
        files = $inventory
    }
    $manifestPath = Join-Path $stage $script:UnboundManifestName
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    Set-UnboundProtectedAcl $stage
    [void](Test-UnboundDeployment $stage -CheckAcl -AllowStaging)
    Move-Item -LiteralPath $stage -Destination $destination
    $published = $true
    [void](Test-UnboundDeployment $destination -CheckAcl)
    Write-Output "Protected deployment installed: $destination"
    Write-Output "Deployment ID: $deploymentId"
    Write-Output "Proxy SHA-256: $($manifest.proxy_sha256)"
    Write-Output "Chromium SHA-256: $($manifest.chromium_sha256)"
} catch {
    # Only remove the unique staging directory created by this invocation.
    if (Test-Path -LiteralPath $stage -PathType Container) {
        $stageItem = Get-Item -LiteralPath $stage -Force
        if ($stageItem.Name -like '.UNBOUND AI.stage-*' -and $stageItem.FullName.StartsWith($env:ProgramFiles, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $stage -Recurse -Force
        }
    }
    if ($published -and (Test-Path -LiteralPath $destination -PathType Container)) {
        try {
            if (Remove-UnboundFailedPublication $destination $deploymentId) {
                Write-Warning 'The newly published deployment failed final verification and was rolled back.'
            } else {
                Write-Warning 'Final verification failed; the published path was retained because ownership could not be confirmed.'
            }
        } catch {
            Write-Warning 'Final verification failed; rollback was not attempted because the new deployment identity could not be confirmed.'
        }
    }
    throw
}
