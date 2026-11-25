[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$Root = (Join-Path $env:ProgramFiles 'UNBOUND AI'),
    [string]$ExpectedDeploymentId
)

. (Join-Path $PSScriptRoot 'protected-deployment-common.ps1')
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $WhatIfPreference -and -not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Protected deployment removal requires an elevated PowerShell session.'
}

$manifest = Test-UnboundDeployment $Root -CheckAcl
if ($ExpectedDeploymentId -and $ExpectedDeploymentId -ne [string]$manifest.deployment_id) {
    throw 'Deployment ID does not match the deployment selected for removal.'
}
if ($PSCmdlet.ShouldProcess($Root, "Remove verified UNBOUND deployment $($manifest.deployment_id)")) {
    $expectedRoot = [IO.Path]::GetFullPath($script:UnboundDeploymentRoot).TrimEnd('\')
    $actualRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($actualRoot -ne $expectedRoot -or (Split-Path $actualRoot -Leaf) -ne 'UNBOUND AI') {
        throw 'Refusing to remove a path outside the fixed protected deployment root.'
    }
    # Test-UnboundDeployment has verified every manifest hash and rejected
    # unexpected files before this exact application-owned directory removal.
    Remove-Item -LiteralPath $actualRoot -Recurse -Force
    if (Test-Path -LiteralPath $actualRoot) { throw 'Protected deployment removal did not complete.' }
    Write-Output "Removed verified deployment $($manifest.deployment_id)."
}
