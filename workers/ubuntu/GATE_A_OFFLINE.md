# Gate A disconnected provisioning

Public browsing stays disabled. Keep the VM cable off, NAT and bridged adapters disabled. This is a renderer-only synthetic test. It does not install or start the broker and does not configure a guest proxy.

## Windows package build

The canonical install directory is /opt/unbound-gate-a. The renderer unit,
broker unit, browser path, virtual environment and acceptance script all use
that directory. The bundle builder includes isolated worker modules, reviewed
systemd units, acceptance checks and SHA-256 verified CPython 3.12 Linux wheels.
It creates empty package initializers to avoid importing the full Windows
backend.

Choose a reviewed Linux wheelhouse and a new output path. The script refuses existing output paths and validates every wheel against the lock.

```powershell
$repo = 'C:\Users\KARTHIK G A\UNBOUND-AI'
$wheelhouse = 'C:\path\to\reviewed-linux-cp312-wheelhouse'
$output = Join-Path $env:TEMP ('unbound-gate-a-' + [guid]::NewGuid().ToString())
& (Join-Path $repo 'workers\ubuntu\package-gate-a.ps1') -Wheelhouse $wheelhouse -OutputDirectory $output
Get-FileHash (Join-Path $output 'unbound-gate-a.zip') -Algorithm SHA256
```

Copy the archive hash through a trusted channel. On Ubuntu compare it before extracting. Verify all relative paths in SHA256SUMS.json. The manifest checks integrity against the package but does not authenticate the original checkout.

## Ubuntu installation

Run only after confirming the VM remains disconnected and the account, group,
service file and install paths do not exist. Require Ubuntu 24.04, Python 3.12
and venv support, systemd, iproute2, util-linux (nsenter), procps (pgrep), and
reviewed Chromium plus its shared libraries. The renderer uses Playwright's
expected browser-cache layout under /opt/unbound-gate-a/chromium. This package
does not contain Chromium archives or prove that cache layout. Do not run apt,
pip index, or Playwright browser downloads.

Likely Ubuntu 24.04 runtime libraries for Playwright Chromium include
libasound2t64, libatk1.0-0t64, libatk-bridge2.0-0t64, libatspi2.0-0t64,
libcairo2, libcups2t64, libdbus-1-3, libdrm2, libgbm1, libglib2.0-0t64,
libnspr4, libnss3, libpango-1.0-0, libx11-6, libxcb1, libxcomposite1,
libxdamage1, libxext6, libxfixes3, libxkbcommon0, libxrandr2 and
libxshmfence1. Fonts may also be needed. This is an unverified candidate list,
not a complete package manifest. Obtain the exact list offline from a matching
Ubuntu 24.04 image using Playwright's dependency inspection and verify package
versions and signatures before adding them to transfer media.

```bash
sudo groupadd --system unbound-ipc
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin unbound-worker
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin unbound-renderer
sudo usermod -aG unbound-ipc unbound-worker
test ! -e /opt/unbound-gate-a
sudo install -d -o root -g root -m 0755 /opt/unbound-gate-a
sudo cp -a /media/unbound-gate-a/unbound-gate-a/. /opt/unbound-gate-a/
sudo sh -c 'printf "%s\\n" unbound-gate-a-disposable-test > /opt/unbound-gate-a/.gate-a-owned'
cd /opt/unbound-gate-a
sudo python3.12 -m venv /opt/unbound-gate-a/.venv-worker
sudo .venv-worker/bin/python -m pip install --no-index --only-binary=:all: --require-hashes --find-links=wheelhouse -r workers/ubuntu/requirements-cp312-linux-x86_64.lock
sudo .venv-worker/bin/python -m pip check
sudo install -d -o root -g root -m 0700 /etc/unbound-browser-worker
sudo install -d -o unbound-worker -g unbound-worker -m 0700 /var/lib/unbound-browser-worker
sudo systemd-analyze verify workers/ubuntu/unbound-renderer.service
test ! -e /etc/systemd/system/unbound-renderer.service
sudo install -o root -g root -m 0644 workers/ubuntu/unbound-renderer.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Create only synthetic marker files and a disposable ledger, using exclusive creation:

```bash
sudo python3 - <<'PY'
from pathlib import Path
import os
os.umask(0o077)
for name in ('app.key', 'server.key', 'proxy.password'):
    with (Path('/etc/unbound-browser-worker') / name).open('xb') as f:
        f.write(b'synthetic Gate A marker only')
PY
sudo -u unbound-worker python3 -c 'import os; os.close(os.open("/var/lib/unbound-browser-worker/replay.sqlite3", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))'
sudo systemctl start unbound-renderer
sudo systemctl show unbound-renderer -p MainPID -p User -p Group -p PrivateNetwork -p RestrictAddressFamilies -p ControlGroup
sudo sh workers/ubuntu/check_renderer_boundary.sh
sudo systemctl stop unbound-renderer
systemctl show unbound-renderer -p MainPID -p ActiveState
```

Expected output:

```text
Completed renderer IPC, fixture navigation, click, observation, screenshot and close.
Disconnected renderer checks passed; host egress and guest firewall were not exercised.
```

The fixture intercepts exactly the synthetic product and details pages, aborts other requests, and must not resolve the .invalid name. The boundary script checks distinct UIDs, socket permissions, renderer namespace/routes, denial of reads to existing synthetic files, Chromium UID/seccomp/NoNewPrivs/no --no-sandbox, fixture behavior and shutdown. These expected results are not claimed until run inside Ubuntu.

## Rollback

Stop the test service and inspect its cgroup before removal. Stop if unexpected files exist or the cgroup is nonempty. Commands apply only to paths created above:

```bash
sudo systemctl stop unbound-renderer.service
systemctl show unbound-renderer.service -p MainPID -p ControlGroup -p ActiveState
systemd-cgls --no-pager /system.slice/unbound-renderer.service
sudo cmp -s workers/ubuntu/unbound-renderer.service /etc/systemd/system/unbound-renderer.service
sudo rm -f /etc/systemd/system/unbound-renderer.service
sudo systemctl daemon-reload
```

This rollback removes only the exact matching renderer unit and leaves the installation tree, venv, Chromium files, marker secrets, replay database, accounts and group untouched. Review their complete ownership and contents separately before any manual deletion. Do not use recursive removal for rollback.

## Limitations

The broker and replay ledger are not running in this renderer-only procedure. Therefore broker peer rejection and live broker-secret permissions remain unverified. The Chromium process checks cover its Linux UID and seccomp mode, not every sandbox layer. The package lacks Ubuntu system packages and Chromium payload, so it is not yet self-contained transfer media. No Gate A result is established until the commands are executed and recorded in the disconnected VM.
