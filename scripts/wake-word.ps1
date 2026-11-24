[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('install', 'install-runtime', 'install-training', 'prepare', 'train', 'evaluate', 'install-model', 'validate', 'run', 'start', 'stop', 'status')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$venvPython = Join-Path $repoRoot '.venv-wake\Scripts\python.exe'
$trainPython = Join-Path $repoRoot '.venv-wake-train\Scripts\python.exe'
$backendUrl = 'http://127.0.0.1:8000/api/wake-word'

if ($Action -in @('install', 'install-runtime')) {
    $pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pythonLauncher) { throw 'Python Launcher (py.exe) is required. Install Python 3.14 x64 without replacing the backend interpreter.' }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & py -3.14 -m venv (Join-Path $repoRoot '.venv-wake')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated .venv-wake environment using Python 3.14.' }
        & $venvPython -m pip install -r (Join-Path $repoRoot 'backend\requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Backend dependencies did not install in .venv-wake.' }
    }
    & $venvPython -m pip install -r (Join-Path $repoRoot 'backend\requirements-wake-word-runtime.txt')
    if ($LASTEXITCODE -ne 0) { throw 'openWakeWord runtime dependencies did not install in .venv-wake.' }
    & $venvPython -c "import sys, openwakeword, sounddevice, onnxruntime; print('Python',sys.version.split()[0]); print('openWakeWord',openwakeword.__version__); print('sounddevice',sounddevice.__version__); print('ONNX Runtime',onnxruntime.__version__)"
    if ($LASTEXITCODE -ne 0) { throw 'Wake runtime imports failed. See the error above.' }
    Write-Host 'Optional runtime installed. No model was downloaded and no microphone was opened.'
    Write-Host 'The existing Porcupine packages are retained in this environment as a legacy fallback.'
    exit 0
}

if ($Action -eq 'install-training') {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'Python Launcher (py.exe) is required.' }
    if (-not (Test-Path -LiteralPath $trainPython)) {
        & py -3.14 -m venv (Join-Path $repoRoot '.venv-wake-train')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create isolated .venv-wake-train using Python 3.14.' }
    }
    & $trainPython -m pip install -r (Join-Path $repoRoot 'backend\requirements-wake-word-training.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Training dependencies did not install in .venv-wake-train.' }
    & $trainPython -c "import sys, importlib.metadata as metadata; from wakeword_trainer.paths import bundled_model_paths; print('Python',sys.version.split()[0]); print('wakeword-trainer',metadata.version('wakeword-trainer')); print('Bundled feature files:'); print(*bundled_model_paths(),sep='\n')"
    if ($LASTEXITCODE -ne 0) { throw 'Training package or its feature models failed to load.' }
    Write-Host 'Training environment installed. No training dataset or ACAV100M corpus was downloaded.'
    Write-Host 'The CPU PyTorch training stack needs roughly 250-300 MB of package downloads on this Windows/Python 3.14 setup.'
    exit 0
}

if ($Action -eq 'prepare') {
    if (-not (Test-Path -LiteralPath $trainPython)) { throw 'Run ./scripts/wake-word.ps1 install-training first.' }
    & $trainPython (Join-Path $repoRoot 'scripts\wake_word_training.py') install-features
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the openWakeWord feature assets.' }
    $dataRoot = Join-Path $repoRoot 'data\wake-word\hey-unbound'
    foreach ($name in @('positive', 'negative', 'heldout-positive', 'heldout-negative')) {
        New-Item -ItemType Directory -Force -Path (Join-Path $dataRoot $name) | Out-Null
    }
    Write-Host "Created local-only audio folders under $dataRoot"
    exit 0
}

if ($Action -in @('train', 'evaluate', 'install-model')) {
    if (-not (Test-Path -LiteralPath $trainPython)) { throw 'Run ./scripts/wake-word.ps1 install-training first.' }
    Push-Location $repoRoot
    try {
        switch ($Action) {
            'train' {
                $dataRoot = Join-Path $repoRoot 'data\wake-word\hey-unbound'
                $confirmation = Read-Host 'Confirm these are consented, license-reviewed recordings and contain no ACAV100M/non-commercial data by typing YES'
                if ($confirmation -cne 'YES') { throw 'Training cancelled; no model was created.' }
                & $trainPython (Join-Path $repoRoot 'scripts\wake_word_training.py') train --positives (Join-Path $dataRoot 'positive') --negatives (Join-Path $dataRoot 'negative') --output (Join-Path $repoRoot 'generated_reports\wake-word\hey-unbound') --confirm-data-rights
            }
            'evaluate' {
                $dataRoot = Join-Path $repoRoot 'data\wake-word\hey-unbound'
                $confirmation = Read-Host 'Confirm the held-out recordings are consented and license-reviewed, and contain no ACAV100M/non-commercial data, by typing YES'
                if ($confirmation -cne 'YES') { throw 'Evaluation cancelled; no model was evaluated.' }
                & $trainPython (Join-Path $repoRoot 'scripts\wake_word_training.py') evaluate --model (Join-Path $repoRoot 'generated_reports\wake-word\hey-unbound\hey-unbound.onnx') --positives (Join-Path $dataRoot 'heldout-positive') --negatives (Join-Path $dataRoot 'heldout-negative') --confirm-data-rights
            }
            'install-model' {
                & $trainPython (Join-Path $repoRoot 'scripts\wake_word_training.py') install-model --model (Join-Path $repoRoot 'generated_reports\wake-word\hey-unbound\hey-unbound.onnx')
            }
        }
        if ($LASTEXITCODE -ne 0) { throw "Wake-word $Action failed; see the message above." }
        exit 0
    }
    finally { Pop-Location }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'Wake runtime is not installed. Run: ./scripts/wake-word.ps1 install'
}

Push-Location $repoRoot
try {
    switch ($Action) {
        'validate' { & $venvPython -m backend.app.wake_word.cli validate; exit $LASTEXITCODE }
        'run' { & $venvPython -m backend.app.wake_word.cli run; exit $LASTEXITCODE }
        'start' { Invoke-RestMethod -Method Post -Uri "$backendUrl/start" | ConvertTo-Json -Depth 4 }
        'stop' { Invoke-RestMethod -Method Post -Uri "$backendUrl/stop" | ConvertTo-Json -Depth 4 }
        'status' { Invoke-RestMethod -Method Get -Uri "$backendUrl/status" | ConvertTo-Json -Depth 4 }
    }
}
finally { Pop-Location }
