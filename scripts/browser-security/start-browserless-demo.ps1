[CmdletBinding()]
param(
    [string]$BackendPort = '8004',
    [string]$FrontendPort = '5177',
    [string]$RuntimeDirectory,
    [string]$AllowedHosts = 'example.com'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $repo '.venv-wake\Scripts\python.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source
$frontend = Join-Path $repo 'frontend'
$vite = Join-Path $frontend 'node_modules\vite\bin\vite.js'

function Assert-FreeLoopbackPort([string]$Port) {
    $number = 0
    if (-not [int]::TryParse($Port, [ref]$number) -or $number -lt 1024 -or $number -gt 65535) {
        throw 'A valid unprivileged local port is required.'
    }
    if (Get-NetTCPConnection -State Listen -LocalPort $number -ErrorAction SilentlyContinue) {
        throw "Local port $number is already occupied; existing processes were left untouched."
    }
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'The project Python environment is unavailable.' }
if (-not (Test-Path -LiteralPath $vite -PathType Leaf)) { throw 'Frontend dependencies are unavailable.' }
Assert-FreeLoopbackPort $BackendPort
Assert-FreeLoopbackPort $FrontendPort

$allowedHostEntries = @($AllowedHosts -split ',' | ForEach-Object { $_.Trim().TrimEnd('.').ToLowerInvariant() } | Where-Object { $_ } | Sort-Object -Unique)
if ($allowedHostEntries.Count -lt 1 -or $allowedHostEntries.Count -gt 12 -or
    @($allowedHostEntries | Where-Object {
        $_ -notmatch '^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$' -or
        $_ -match '^\d{1,3}(?:\.\d{1,3}){3}$'
    }).Count -gt 0) {
    throw 'AllowedHosts must contain 1–12 exact DNS hostnames. Wildcards, URLs and IP literals are not accepted.'
}
$allowedHostValue = $allowedHostEntries -join ','

$secureToken = Read-Host 'Enter Browserless API token (input is hidden)' -AsSecureString
if (-not $secureToken -or $secureToken.Length -lt 8) { throw 'A Browserless token is required.' }
$endpointInput = Read-Host 'Browserless Playwright WSS endpoint [wss://production-sfo.browserless.io/chromium/playwright]'
$endpoint = if ([string]::IsNullOrWhiteSpace($endpointInput)) {
    'wss://production-sfo.browserless.io/chromium/playwright'
} else {
    $endpointInput.Trim()
}
if ($endpoint -notmatch '^wss://[^/?#:@]+(?::443)?/chromium/playwright$') {
    $secureToken.Dispose()
    throw 'Endpoint must be a credential-free wss://host/chromium/playwright URL on port 443.'
}

$ownerSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'UNBOUND'
try {
if ([string]::IsNullOrWhiteSpace($RuntimeDirectory)) {
    $localRoot = Join-Path $runtimeRoot ('browserless-demo-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $localRoot | Out-Null
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($ownerSid, 'S-1-5-18', 'S-1-5-32-544')) {
        $identity = [System.Security.Principal.SecurityIdentifier]::new($sid)
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $identity,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit,
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $localRoot -AclObject $acl
} else {
    $localRoot = [System.IO.Path]::GetFullPath($RuntimeDirectory)
    $expectedParent = [System.IO.Path]::GetFullPath($runtimeRoot).TrimEnd('\')
    $actualParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $localRoot)).TrimEnd('\')
    $leaf = Split-Path -Leaf $localRoot
    if (-not [string]::Equals($actualParent, $expectedParent, [StringComparison]::OrdinalIgnoreCase) -or
        $leaf -notmatch '^browserless-demo-[0-9a-f]{32}$' -or
        -not (Test-Path -LiteralPath $localRoot -PathType Container)) {
        throw 'RuntimeDirectory must be an existing direct browserless-demo child of the current user UNBOUND data directory.'
    }
    $rootItem = Get-Item -LiteralPath $localRoot -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'RuntimeDirectory cannot be a reparse point.'
    }
    $acl = Get-Acl -LiteralPath $localRoot
    if (-not $acl.AreAccessRulesProtected -or
        -not [string]::Equals($acl.Owner, [System.Security.Principal.WindowsIdentity]::GetCurrent().Name, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'RuntimeDirectory owner or protected ACL does not match the current user; existing permissions were left unchanged.'
    }
    $expectedSids = @($ownerSid, 'S-1-5-18', 'S-1-5-32-544') | Sort-Object -Unique
    $rules = @($acl.Access)
    $actualSids = @($rules | ForEach-Object { $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } | Sort-Object -Unique)
    if ($rules.Count -ne 3 -or (Compare-Object $expectedSids $actualSids).Count -ne 0 -or
        @($rules | Where-Object {
            $_.IsInherited -or $_.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
            -not ($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl)
        }).Count -gt 0) {
        throw 'RuntimeDirectory ACL is not the expected owner/SYSTEM/Administrators-only Full Control policy; permissions were not changed.'
    }
    $reparseChildren = @(Get-ChildItem -LiteralPath $localRoot -Force -Recurse | Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    })
    if ($reparseChildren.Count -gt 0) {
        throw 'RuntimeDirectory contains a reparse point; existing files were left unchanged.'
    }
    $oldManifests = @(Get-ChildItem -LiteralPath $localRoot -File -Filter 'processes*.json' -ErrorAction Stop)
    if ($oldManifests.Count -eq 0) {
        throw 'RuntimeDirectory has no Browserless process manifest; refusing to reuse an unrecognized directory.'
    }
    foreach ($manifest in $oldManifests) {
        try { $oldRun = Get-Content -LiteralPath $manifest.FullName -Raw | ConvertFrom-Json } catch {
            throw 'RuntimeDirectory contains an invalid process manifest; existing files were left unchanged.'
        }
        foreach ($priorPid in @($oldRun.backend_pid, $oldRun.frontend_pid)) {
            if (-not $priorPid) { continue }
            $prior = Get-CimInstance Win32_Process -Filter "ProcessId = $priorPid" -ErrorAction SilentlyContinue
            if ($prior -and $prior.CommandLine -match 'backend\.app\.main:app|frontend\\node_modules\\vite\\bin\\vite\.js') {
                throw 'A process recorded for RuntimeDirectory is still active; stop only that verified demo instance before reusing its state.'
            }
        }
    }
}
} catch {
    $secureToken.Dispose()
    throw
}

$runId = [guid]::NewGuid().ToString('N')
$outLog = Join-Path $localRoot "backend.stdout-$runId.log"
$errLog = Join-Path $localRoot "backend.stderr-$runId.log"
$frontOut = Join-Path $localRoot "frontend.stdout-$runId.log"
$frontErr = Join-Path $localRoot "frontend.stderr-$runId.log"
$names = @(
    'BROWSERLESS_API_TOKEN','BROWSERLESS_WS_ENDPOINT','BROWSERLESS_ALLOWED_HOSTS','UNBOUND_BROWSER_PROVIDER',
    'UNBOUND_BROWSER_LOCAL_DEMO','UNBOUND_TASK_DB','UNBOUND_CONVERSATION_DB','UNBOUND_DOCUMENT_STORAGE',
    'UNBOUND_BROWSER_WORKFLOW_DIR','LLM_PROVIDER','LLM_MODEL','LLM_BASE_URL','LLM_TIMEOUT_SECONDS',
    'VITE_BACKEND_PROXY_TARGET'
)
$old = @{}
foreach ($name in $names) { $old[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$backendProcess = $null
$frontendProcess = $null
$ptr = [IntPtr]::Zero
$plainToken = $null
try {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    [Environment]::SetEnvironmentVariable('BROWSERLESS_API_TOKEN', $plainToken, 'Process')
    [Environment]::SetEnvironmentVariable('BROWSERLESS_WS_ENDPOINT', $endpoint, 'Process')
    [Environment]::SetEnvironmentVariable('BROWSERLESS_ALLOWED_HOSTS', $allowedHostValue, 'Process')
    [Environment]::SetEnvironmentVariable('UNBOUND_BROWSER_PROVIDER', 'browserless', 'Process')
    [Environment]::SetEnvironmentVariable('UNBOUND_BROWSER_LOCAL_DEMO', $null, 'Process')
    [Environment]::SetEnvironmentVariable('UNBOUND_TASK_DB', (Join-Path $localRoot 'tasks.sqlite3'), 'Process')
    [Environment]::SetEnvironmentVariable('UNBOUND_CONVERSATION_DB', (Join-Path $localRoot 'conversations.sqlite3'), 'Process')
    [Environment]::SetEnvironmentVariable('UNBOUND_DOCUMENT_STORAGE', (Join-Path $localRoot 'documents'), 'Process')
    [Environment]::SetEnvironmentVariable('UNBOUND_BROWSER_WORKFLOW_DIR', (Join-Path $localRoot 'workflows'), 'Process')
    [Environment]::SetEnvironmentVariable('LLM_PROVIDER', 'ollama', 'Process')
    [Environment]::SetEnvironmentVariable('LLM_MODEL', 'qwen3.5:4b', 'Process')
    [Environment]::SetEnvironmentVariable('LLM_BASE_URL', 'http://127.0.0.1:11434/v1', 'Process')
    [Environment]::SetEnvironmentVariable('LLM_TIMEOUT_SECONDS', '120', 'Process')
    $backendArgs = @('-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port',$BackendPort)
    $backendProcess = Start-Process -FilePath $python -ArgumentList $backendArgs -WorkingDirectory $repo -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog

    [Environment]::SetEnvironmentVariable('BROWSERLESS_API_TOKEN', $null, 'Process')
    [Environment]::SetEnvironmentVariable('VITE_BACKEND_PROXY_TARGET', "http://127.0.0.1:$BackendPort", 'Process')
    $frontendArgs = '"{0}" --host 127.0.0.1 --port {1} --strictPort' -f $vite, $FrontendPort
    $frontendProcess = Start-Process -FilePath $node -ArgumentList $frontendArgs -WorkingDirectory $frontend -WindowStyle Hidden -PassThru -RedirectStandardOutput $frontOut -RedirectStandardError $frontErr

    $healthUri = "http://127.0.0.1:$BackendPort/api/health"
    $health = $null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try { $health = Invoke-RestMethod -Uri $healthUri -TimeoutSec 2; break } catch { if ($backendProcess.HasExited) { break } }
    }
    if (-not $health -or -not $health.browserless_cloud_demo.configured) {
        throw 'Browserless backend failed its configuration readiness check. Review sanitized backend logs in the private demo directory.'
    }
    $record = [ordered]@{
        backend_pid = $backendProcess.Id
        frontend_pid = $frontendProcess.Id
        backend_port = [int]$BackendPort
        frontend_port = [int]$FrontendPort
        started_at = [DateTimeOffset]::UtcNow.ToString('o')
    }
    $record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $localRoot "processes-$runId.json") -Encoding UTF8
    Write-Output "Browserless demo services started. UI: http://127.0.0.1:$FrontendPort"
    Write-Output "Backend: 127.0.0.1:$BackendPort; provider configured; session not tested until a task runs."
    Write-Output "Private logs and isolated demo data: $localRoot"
    Write-Output "Self-hosted Windows/Ubuntu containment gates remain separate and unverified."
} catch {
    if ($frontendProcess) { Stop-Process -Id $frontendProcess.Id -ErrorAction SilentlyContinue }
    if ($backendProcess) { Stop-Process -Id $backendProcess.Id -ErrorAction SilentlyContinue }
    throw
} finally {
    if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    $plainToken = $null
    $secureToken.Dispose()
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $old[$name], 'Process') }
}
