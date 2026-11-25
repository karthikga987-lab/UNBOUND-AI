$ErrorActionPreference = 'Stop'
$testRoot = Join-Path $env:TEMP ('unbound-protected-deployment-test-' + [guid]::NewGuid().ToString('N'))
$fakeProgramFiles = Join-Path $testRoot 'Program Files'
$common = Join-Path $PSScriptRoot '..\protected-deployment-common.ps1'
$linkTarget = Join-Path $testRoot 'target'
$junction = Join-Path $testRoot 'stage\linked'
$script:Assertions = 0

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:Assertions++
}

try {
    New-Item -ItemType Directory -Path $fakeProgramFiles, $linkTarget | Out-Null
    $env:ProgramFiles = $fakeProgramFiles
    . $common

    New-Item -ItemType Directory -Path (Split-Path $junction -Parent) | Out-Null
    try {
        New-Item -ItemType Junction -Path $junction -Target $linkTarget | Out-Null
        $reparseRejected = $false
        try { Assert-UnboundNoReparsePoint (Split-Path $junction -Parent) } catch { $reparseRejected = $true }
        Assert-True $reparseRejected 'Staging reparse-point validation did not reject a junction.'
    } catch {
        if (-not (Test-Path -LiteralPath $junction)) {
            throw 'Could not create a synthetic junction to verify reparse-point rejection.'
        }
        throw
    }

    $stageRoot = Join-Path $fakeProgramFiles ('.UNBOUND AI.stage-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stageRoot | Out-Null
    $payload = Join-Path $stageRoot 'payload.bin'
    Set-Content -LiteralPath $payload -Value 'synthetic' -NoNewline
    $hash = Get-UnboundFileHash $payload
    $stageManifest = @{
        schema_version = 1
        application = 'UNBOUND-AI'
        deployment_id = [guid]::NewGuid().ToString('D')
        files = @(@{ path = 'payload.bin'; sha256 = $hash })
        proxy_path = 'payload.bin'
        proxy_sha256 = $hash
        chromium_path = 'payload.bin'
        chromium_sha256 = $hash
    }
    $stageManifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $stageRoot 'deployment.json') -Encoding UTF8
    [void](Test-UnboundDeployment $stageRoot -AllowStaging)
    $stageManifest.files[0].path = 'payload/../payload.bin'
    $stageManifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $stageRoot 'deployment.json') -Encoding UTF8
    $canonicalRejected = $false
    try { [void](Test-UnboundDeployment $stageRoot -AllowStaging) } catch { $canonicalRejected = $true }
    Assert-True $canonicalRejected 'Manifest path traversal was not rejected by PowerShell validation.'

    $fixedRoot = Join-Path $fakeProgramFiles 'UNBOUND AI'
    New-Item -ItemType Directory -Path $fixedRoot | Out-Null
    $ownedId = [guid]::NewGuid().ToString('D')
    $manifest = @{ application = 'UNBOUND-AI'; deployment_id = $ownedId } | ConvertTo-Json
    Set-Content -LiteralPath (Join-Path $fixedRoot 'deployment.json') -Value $manifest -Encoding UTF8
    $wrongIdRetained = -not (Remove-UnboundFailedPublication $fixedRoot ([guid]::NewGuid().ToString('D')))
    Assert-True ($wrongIdRetained -and (Test-Path -LiteralPath $fixedRoot)) 'Rollback removed a deployment with a mismatched ID.'
    $removedOwned = Remove-UnboundFailedPublication $fixedRoot $ownedId
    Assert-True ($removedOwned -and -not (Test-Path -LiteralPath $fixedRoot)) 'Rollback did not remove the exact newly owned deployment.'

    Write-Output "Protected deployment PowerShell safety tests passed: $script:Assertions assertions."
} finally {
    $resolvedTemp = [IO.Path]::GetFullPath($testRoot).TrimEnd('\')
    $resolvedParent = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
    if ($resolvedTemp.StartsWith($resolvedParent, [StringComparison]::OrdinalIgnoreCase) -and (Split-Path $resolvedTemp -Leaf).StartsWith('unbound-protected-deployment-test-')) {
        if (Test-Path -LiteralPath $resolvedTemp) { Remove-Item -LiteralPath $resolvedTemp -Recurse -Force }
    }
}
