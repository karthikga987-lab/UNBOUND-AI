[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('install', 'start', 'stop', 'status', 'uninstall', 'run')]
    [string]$Action,
    [string]$Python = 'python',
    [string]$DatabasePath
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$TaskName = 'UNBOUND Local Reminder Notifications'
$InstallDir = Join-Path $env:LOCALAPPDATA 'UNBOUND_AI'
$InstallMarker = Join-Path $InstallDir 'notification-agent.install.json'
$Heartbeat = Join-Path $InstallDir 'notification-agent.heartbeat.json'

function Get-PythonExecutable {
    $command = Get-Command $Python -ErrorAction Stop
    return $command.Source
}

function Get-DatabasePath {
    if ($DatabasePath) { return $DatabasePath }
    $value = & $Python -c "from backend.app.conversation.store import default_database_path; print(default_database_path())"
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the configured reminder database path.' }
    return ($value | Select-Object -Last 1).Trim()
}

function Get-AgentStatus {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $installInfo = $null
    if (Test-Path -LiteralPath $InstallMarker) {
        try { $installInfo = Get-Content -LiteralPath $InstallMarker -Raw | ConvertFrom-Json } catch { $installInfo = $null }
    }
    $installed = $null -ne $installInfo
    $running = $false
    if ($task) { $running = $task.State -eq 'Running' }
    $heartbeatFresh = $false
    if (Test-Path -LiteralPath $Heartbeat) {
        try {
            $beat = Get-Content -LiteralPath $Heartbeat -Raw | ConvertFrom-Json
            $age = ([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($beat.heartbeat_at)).TotalSeconds
            $heartbeatFresh = ($age -ge 0 -and $age -le ([int]$beat.poll_interval_seconds * 3 + 15))
        } catch { $heartbeatFresh = $false }
    }
    [pscustomobject]@{ Installed = $installed -and ($null -ne $task); ScheduledTaskState = if ($task) { $task.State } else { 'Not registered' }; HeartbeatFresh = $heartbeatFresh; DatabasePath = if ($installInfo) { $installInfo.database_path } else { 'Not configured' } }
}

switch ($Action) {
    'install' {
        $pythonExe = Get-PythonExecutable
        Push-Location $RepoRoot
        try {
            & $Python -c "import win11toast, pythoncom; from win32com.propsys import propsys" 2>$null
            if ($LASTEXITCODE -ne 0) { throw 'Install optional dependencies first: python -m pip install -r backend/requirements-windows-notifications.txt' }
            $pythonw = Join-Path (Split-Path $pythonExe) 'pythonw.exe'
            if (-not (Test-Path -LiteralPath $pythonw)) { throw 'Could not find pythonw.exe beside the selected Python interpreter.' }
            $dbPath = Get-DatabasePath
            & $Python -m backend.app.reminders.windows_identity install --pythonw $pythonw --project-root $RepoRoot
            if ($LASTEXITCODE -ne 0) { throw 'Could not create the Start Menu notification identity shortcut.' }
            $argument = "-m backend.app.reminders.agent --database-path `"$dbPath`""
            $actionObject = New-ScheduledTaskAction -Execute $pythonw -Argument $argument -WorkingDirectory $RepoRoot
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
            $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
            Register-ScheduledTask -TaskName $TaskName -Action $actionObject -Trigger $trigger -Principal $principal -Settings $settings -Description 'Shows locally stored UNBOUND reminders in the logged-in Windows session.' -Force | Out-Null
            New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
            [ordered]@{ app_id = 'UNBOUND.AI.Reminders'; installed_at = [DateTimeOffset]::UtcNow.ToString('o'); database_path = $dbPath } | ConvertTo-Json | Set-Content -LiteralPath $InstallMarker -Encoding utf8
            Start-ScheduledTask -TaskName $TaskName
            Write-Output "Installed and started '$TaskName'. Database: $dbPath"
        } finally { Pop-Location }
    }
    'start' { Start-ScheduledTask -TaskName $TaskName; Write-Output 'Reminder agent started.' }
    'stop' { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue; Write-Output 'Reminder agent stopped.' }
    'status' { Get-AgentStatus | Format-List }
    'uninstall' {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Push-Location $RepoRoot
        try { & $Python -m backend.app.reminders.windows_identity uninstall } finally { Pop-Location }
        Remove-Item -LiteralPath $InstallMarker -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $Heartbeat -Force -ErrorAction SilentlyContinue
        Write-Output 'Removed the Scheduled Task, Start Menu shortcut and install marker. Reminder data and delivery history were preserved.'
    }
    'run' {
        Push-Location $RepoRoot
        try {
            $dbPath = Get-DatabasePath
            & $Python -m backend.app.reminders.agent --database-path $dbPath
        } finally { Pop-Location }
    }
}
