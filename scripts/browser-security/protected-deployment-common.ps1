Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:UnboundDeploymentRoot = Join-Path $env:ProgramFiles 'UNBOUND AI'
$script:UnboundManifestName = 'deployment.json'
$script:UnboundAdminSid = 'S-1-5-32-544'
$script:UnboundSystemSid = 'S-1-5-18'
$script:UnboundUsersSid = 'S-1-5-32-545'

function Get-UnboundFileHash([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-UnboundInventory([string]$Root) {
    $resolved = (Get-Item -LiteralPath $Root -Force).FullName
    $items = Get-ChildItem -LiteralPath $resolved -Force -Recurse
    foreach ($item in $items) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Reparse points are not allowed in a protected deployment: $($item.FullName)"
        }
    }
    @(
        foreach ($file in $items | Where-Object { -not $_.PSIsContainer } | Sort-Object FullName) {
            $fullPath = $file.FullName
            if (-not $fullPath.StartsWith($resolved.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
                throw 'A deployment file resolved outside its inventory root.'
            }
            [pscustomobject]@{
                path = $fullPath.Substring($resolved.TrimEnd('\').Length + 1).Replace('\', '/')
                sha256 = Get-UnboundFileHash $fullPath
            }
        }
    )
}

function Assert-UnboundNoReparsePoint([string]$Path) {
    $rootItem = Get-Item -LiteralPath $Path -Force
    if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Source contains a reparse point.'
    }
    if (-not $rootItem.PSIsContainer) { return }
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($rootItem.FullName)
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        foreach ($child in Get-ChildItem -LiteralPath $current -Force) {
            if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Source contains a reparse point.'
            }
            if ($child.PSIsContainer) { $pending.Push($child.FullName) }
        }
    }
}

function Remove-UnboundFailedPublication([string]$Root, [string]$DeploymentId) {
    $expectedRoot = [IO.Path]::GetFullPath($script:UnboundDeploymentRoot).TrimEnd('\')
    $actualRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($actualRoot -ne $expectedRoot) { return $false }
    $manifestPath = Join-Path $actualRoot $script:UnboundManifestName
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return $false }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.application -ne 'UNBOUND-AI' -or [string]$manifest.deployment_id -ne $DeploymentId) { return $false }
    # Called only after this invocation published a previously absent root.
    Remove-Item -LiteralPath $actualRoot -Recurse -Force
    if (Test-Path -LiteralPath $actualRoot) { throw 'Rollback did not remove the failed deployment.' }
    return $true
}

function Set-UnboundProtectedAcl([string]$Root) {
    $admins = [Security.Principal.SecurityIdentifier]::new($script:UnboundAdminSid)
    $system = [Security.Principal.SecurityIdentifier]::new($script:UnboundSystemSid)
    $users = [Security.Principal.SecurityIdentifier]::new($script:UnboundUsersSid)
    $items = @((Get-Item -LiteralPath $Root -Force)) + @(Get-ChildItem -LiteralPath $Root -Force -Recurse)
    foreach ($item in $items) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Reparse points are not allowed while applying protected ACLs.'
        }
        if ($item.PSIsContainer) {
            $acl = [Security.AccessControl.DirectorySecurity]::new()
            $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
        } else {
            $acl = [Security.AccessControl.FileSecurity]::new()
            $inheritance = [Security.AccessControl.InheritanceFlags]::None
        }
        $acl.SetAccessRuleProtection($true, $false)
        $acl.SetOwner($admins)
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($admins, [Security.AccessControl.FileSystemRights]::FullControl, $inheritance, [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow))
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($system, [Security.AccessControl.FileSystemRights]::FullControl, $inheritance, [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow))
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($users, [Security.AccessControl.FileSystemRights]::ReadAndExecute, $inheritance, [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow))
        Set-Acl -LiteralPath $item.FullName -AclObject $acl
    }
}

function Test-UnboundProtectedAcl([string]$Root) {
    $admins = [Security.Principal.SecurityIdentifier]::new($script:UnboundAdminSid)
    $system = [Security.Principal.SecurityIdentifier]::new($script:UnboundSystemSid)
    $users = [Security.Principal.SecurityIdentifier]::new($script:UnboundUsersSid)
    $requiredPrivileged = @($script:UnboundAdminSid, $script:UnboundSystemSid)
    $items = @((Get-Item -LiteralPath $Root -Force)) + @(Get-ChildItem -LiteralPath $Root -Force -Recurse)
    foreach ($item in $items) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
        $acl = Get-Acl -LiteralPath $item.FullName
        $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($ownerSid -ne $admins.Value -and $ownerSid -ne $system.Value) { return $false }
        if (-not $acl.AreAccessRulesProtected) { return $false }
        $rules = @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
        $allowedSids = @($script:UnboundAdminSid, $script:UnboundSystemSid, $script:UnboundUsersSid)
        if (@($rules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) { return $false }
        if (@($rules | Where-Object { $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Deny }).Count -gt 0) { return $false }
        $privilegedFull = @($rules | Where-Object {
            $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            $_.IdentityReference.Value -in $requiredPrivileged -and
            (($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq [Security.AccessControl.FileSystemRights]::FullControl)
        } | ForEach-Object { $_.IdentityReference.Value } | Select-Object -Unique)
        if ($privilegedFull.Count -ne 2) { return $false }
        $writeMask = [Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Modify -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership
        if (@($rules | Where-Object {
            $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            $_.IdentityReference.Value -eq $script:UnboundUsersSid -and
            (($_.FileSystemRights -band $writeMask) -ne 0)
        }).Count -gt 0) { return $false }
        if (@($rules | Where-Object {
            $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            $_.IdentityReference.Value -eq $script:UnboundUsersSid -and
            (($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::ReadAndExecute) -eq [Security.AccessControl.FileSystemRights]::ReadAndExecute)
        }).Count -eq 0) { return $false }
    }
    return $true
}

function Test-UnboundDeployment([string]$Root, [switch]$CheckAcl, [switch]$AllowStaging) {
    $expectedRoot = [IO.Path]::GetFullPath($script:UnboundDeploymentRoot).TrimEnd('\')
    $actualRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $programFiles = [IO.Path]::GetFullPath($env:ProgramFiles).TrimEnd('\')
    if ($AllowStaging) {
        if ((Split-Path $actualRoot -Parent) -ne $programFiles -or (Split-Path $actualRoot -Leaf) -notmatch '^\.UNBOUND AI\.stage-[0-9a-f]{32}$') {
            throw 'Staging path does not match the owned Program Files staging pattern.'
        }
    } elseif ($actualRoot -ne $expectedRoot) {
        throw 'Protected deployment root does not match the fixed Program Files location.'
    }
    $manifestPath = Join-Path $actualRoot $script:UnboundManifestName
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Protected deployment manifest is missing.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or $manifest.application -ne 'UNBOUND-AI' -or [string]::IsNullOrWhiteSpace($manifest.deployment_id)) {
        throw 'Protected deployment manifest is invalid.'
    }
    $expectedFiles = @($manifest.files | ForEach-Object { [string]$_.path })
    if ($expectedFiles.Count -eq 0 -or @($expectedFiles | Sort-Object -Unique).Count -ne $expectedFiles.Count) {
        throw 'Protected deployment file inventory is invalid.'
    }
    foreach ($entry in $manifest.files) {
        $relative = [string]$entry.path
        $segments = $relative.Split('/')
        if (
            [IO.Path]::IsPathRooted($relative) -or
            $relative -match '[\\:*?"<>|]' -or
            $relative.Contains(':') -or
            (@($segments | Where-Object { $_ -in @('', '.', '..') -or $_.EndsWith('.') -or $_.EndsWith(' ') -or $_ -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$' }).Count -gt 0)
        ) { throw 'Manifest contains an unsafe relative path.' }
        $path = Join-Path $actualRoot ($relative.Replace('/', '\'))
        $canonicalPath = [IO.Path]::GetFullPath($path)
        if (-not $canonicalPath.StartsWith($actualRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Manifest path escapes the deployment root.' }
        $canonicalRelative = $canonicalPath.Substring($actualRoot.TrimEnd('\').Length + 1).Replace('\', '/')
        if (-not [string]::Equals($relative, $canonicalRelative, [StringComparison]::Ordinal)) { throw 'Manifest path is not canonical.' }
        $walk = $actualRoot
        foreach ($segment in $segments) {
            $walk = Join-Path $walk $segment
            $component = Get-Item -LiteralPath $walk -Force -ErrorAction SilentlyContinue
            if ($null -ne $component -and ($component.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Manifest path contains a reparse point.' }
        }
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Protected deployment file is missing: $relative" }
        if ((Get-UnboundFileHash $path) -ne ([string]$entry.sha256).ToLowerInvariant()) { throw "Protected deployment hash mismatch: $relative" }
    }
    $actualFiles = @(Get-UnboundInventory $actualRoot | Where-Object { $_.path -ne $script:UnboundManifestName })
    if ($actualFiles.Count -ne $expectedFiles.Count) { throw 'Protected deployment contains unexpected or missing files.' }
    if ($manifest.proxy_path -notin $expectedFiles -or $manifest.chromium_path -notin $expectedFiles) { throw 'Manifest executable paths are not in its inventory.' }
    $proxy = Join-Path $actualRoot ($manifest.proxy_path.Replace('/', '\'))
    $chrome = Join-Path $actualRoot ($manifest.chromium_path.Replace('/', '\'))
    if ((Get-UnboundFileHash $proxy) -ne ([string]$manifest.proxy_sha256).ToLowerInvariant()) { throw 'Protected proxy hash does not match the manifest.' }
    if ((Get-UnboundFileHash $chrome) -ne ([string]$manifest.chromium_sha256).ToLowerInvariant()) { throw 'Protected Chromium hash does not match the manifest.' }
    if ($CheckAcl -and -not (Test-UnboundProtectedAcl $actualRoot)) { throw 'Protected deployment ACL verification failed.' }
    return $manifest
}
