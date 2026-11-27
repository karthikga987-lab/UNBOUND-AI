[CmdletBinding()]
param(
    [switch]$NetworkApproved,
    [string]$IdentityDirectory
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$vpython = Join-Path $repo '.venv-wake\Scripts\python.exe'
$identityRoot = Join-Path $env:LOCALAPPDATA 'UNBOUND\worker-dev'
$dataRoot = Join-Path $env:LOCALAPPDATA ('UNBOUND\isolated-worker-demo-' + [guid]::NewGuid().ToString('N'))
$backendPort = 8002
$frontendPort = 5174

function Assert-PrivateAcl([string]$Path, [string]$ExpectedOwnerSid, [switch]$RequireProtected) {
    $acl = Get-Acl -LiteralPath $Path
    if ($RequireProtected -and -not $acl.AreAccessRulesProtected) {
        throw "ACL inheritance is enabled for a protected path: $Path"
    }
    $allowedSids = @($ExpectedOwnerSid, 'S-1-5-18', 'S-1-5-32-544')
    $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($rule in $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
        $sid = $rule.IdentityReference.Value
        if ($sid -notin $allowedSids -or $rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {
            throw "Unexpected identity or deny rule on protected path: $Path"
        }
        if (($rule.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -ne [System.Security.AccessControl.FileSystemRights]::FullControl) {
            throw "Protected identity ACL is not FullControl: $Path"
        }
        [void]$seen.Add($sid)
    }
    foreach ($sid in $allowedSids) {
        if (-not $seen.Contains($sid)) { throw "Required protected identity is missing on: $Path" }
    }
}

if (-not $NetworkApproved) {
    throw 'No processes were started. Review the host-only adapter and firewall change, then rerun with -NetworkApproved only after approving that exact change.'
}
if (-not (Test-Path -LiteralPath $vpython -PathType Leaf)) { throw 'Project Python environment is unavailable.' }
if (-not $IdentityDirectory) {
    $candidate = Get-ChildItem -LiteralPath $identityRoot -Directory -ErrorAction Stop |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if (-not $candidate) { throw 'No development worker identity bundle was found.' }
    $IdentityDirectory = $candidate.FullName
}
$identityDirectory = (Resolve-Path -LiteralPath $IdentityDirectory).Path
$ownerSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
Assert-PrivateAcl -Path $identityDirectory -ExpectedOwnerSid $ownerSid -RequireProtected
$manifest = Get-Content -LiteralPath (Join-Path $identityDirectory 'manifest.json') -Raw | ConvertFrom-Json
if ($manifest.purpose -ne 'development only' -or [datetimeoffset]::Parse($manifest.expires_at) -le [datetimeoffset]::UtcNow) {
    throw 'Worker development identity is expired or has an unexpected purpose.'
}
foreach ($name in @('host-signing-key.pem','host-client-key.pem','gateway-server-key.pem','worker-server-key.pem','broker-client-key.pem','worker-signing-key.pem','proxy.password','transfer.key')) {
    $path = Join-Path $identityDirectory $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Worker identity bundle is incomplete.' }
    Assert-PrivateAcl -Path $path -ExpectedOwnerSid $ownerSid
}

foreach ($port in @($backendPort, $frontendPort)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Required isolated demo port $port is already occupied. Existing processes were left untouched."
    }
}
$vite = Join-Path $repo 'frontend\node_modules\.bin\vite.cmd'
if (-not (Test-Path -LiteralPath $vite -PathType Leaf)) { throw 'Frontend dependencies are not installed.' }

# The service listener uses the host-only NIC and does not touch the existing
# loopback proxy at 127.0.0.1:18765. A host firewall rule and VM cable change
# must be applied separately after the exact scope is reviewed and approved.
$probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse('192.168.56.1'), 18765)
try { $probe.Start() } catch { throw 'The dedicated host-only gateway endpoint is occupied or unavailable.' } finally { $probe.Stop() }

New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
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
Set-Acl -LiteralPath $dataRoot -AclObject $acl

$logDir = Join-Path $dataRoot 'logs'
New-Item -ItemType Directory -Path $logDir | Out-Null
$backendOut = Join-Path $logDir 'backend.stdout.log'
$backendErr = Join-Path $logDir 'backend.stderr.log'
$frontendOut = Join-Path $logDir 'frontend.stdout.log'
$frontendErr = Join-Path $logDir 'frontend.stderr.log'

$oldEnv = @{}
function Set-DemoEnv([string]$Name, [string]$Value) {
    if (-not $script:oldEnv.ContainsKey($Name)) { $script:oldEnv[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process') }
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

try {
    Set-DemoEnv 'LLM_PROVIDER' 'ollama'
    Set-DemoEnv 'LLM_MODEL' 'qwen3.5:4b'
    Set-DemoEnv 'LLM_BASE_URL' 'http://localhost:11434/v1'
    Set-DemoEnv 'LLM_TIMEOUT_SECONDS' '120'
    Set-DemoEnv 'UNBOUND_TASK_DB' (Join-Path $dataRoot 'tasks.sqlite3')
    Set-DemoEnv 'UNBOUND_CONVERSATION_DB' (Join-Path $dataRoot 'conversations.sqlite3')
    Set-DemoEnv 'UNBOUND_DOCUMENT_STORAGE' (Join-Path $dataRoot 'documents')
    Set-DemoEnv 'UNBOUND_BROWSER_WORKFLOW_DIR' (Join-Path $dataRoot 'workflows')
    Set-DemoEnv 'UNBOUND_BROWSER_REPLAY_STATE_DIR' (Join-Path $dataRoot 'replay')
    Set-DemoEnv 'UNBOUND_BROWSER_EXECUTION_MODE' 'isolated-worker'
    Set-DemoEnv 'UNBOUND_WORKER_IDENTITY_DIRECTORY' $identityDirectory
    Set-DemoEnv 'UNBOUND_WORKER_ENDPOINT' 'https://192.168.56.10:9443'
    Set-DemoEnv 'UNBOUND_WORKER_ID' 'unbound-ubuntu-worker'
    Set-DemoEnv 'UNBOUND_FETCH_GATEWAY_ENABLED' '1'
    Set-DemoEnv 'UNBOUND_FETCH_GATEWAY_TOKEN_FILE' (Join-Path $identityDirectory 'proxy.password')
    # Explicit Cloudflare DoT configuration; no Windows system DNS fallback.
    Set-DemoEnv 'UNBOUND_DOT_RESOLVER_IP' '1.1.1.1'
    Set-DemoEnv 'UNBOUND_DOT_TLS_SERVER_NAME' 'cloudflare-dns.com'
    Set-DemoEnv 'UNBOUND_DOT_TIMEOUT_SECONDS' '3'

    $backend = Start-Process -FilePath $vpython -ArgumentList @('-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port',[string]$backendPort) -WorkingDirectory $repo -WindowStyle Hidden -PassThru -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr
    Set-DemoEnv 'VITE_BACKEND_PROXY_TARGET' "http://127.0.0.1:$backendPort"
    $frontend = Start-Process -FilePath $env:ComSpec -ArgumentList @('/d','/s','/c',"`"$vite`" --host 127.0.0.1 --port $frontendPort --strictPort") -WorkingDirectory (Join-Path $repo 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr
} finally {
    foreach ($entry in $oldEnv.GetEnumerator()) { [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process') }
}

$health = $null
$deadline = [datetime]::UtcNow.AddSeconds(45)
do {
    Start-Sleep -Milliseconds 400
    try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$backendPort/api/health" -TimeoutSec 2 } catch { }
} while (-not $health -and [datetime]::UtcNow -lt $deadline)
if (-not $health) { throw "Isolated backend did not become healthy; logs are at $logDir" }

[pscustomobject]@{
    BackendUrl = "http://127.0.0.1:$backendPort"
    FrontendUrl = "http://127.0.0.1:$frontendPort"
    BackendPid = $backend.Id
    FrontendPid = $frontend.Id
    DataDirectory = $dataRoot
    WorkerConfigured = $health.browser_worker.configured
    WorkerReachable = $health.browser_worker.reachable
    WorkerAuthenticated = $health.browser_worker.authenticated
    WorkerExecutionEnabled = $health.browser_worker.execution_enabled
    PublicBrowsingEnabled = $health.browser_public_execution_enabled
    WorkerFailureCategory = $health.browser_worker.failure_category
} | Format-List
