# Disconnected Ubuntu browser worker

## Release decision

Public browsing is disabled. The host-side authenticated worker client, TaskExecutor
dispatcher, and restricted HTTPS fetch gateway are implemented in the working
tree and selected only with `UNBOUND_BROWSER_EXECUTION_MODE=isolated-worker`.
Remote dispatch remains fail-closed until independent OS network-containment
verification exists. The Windows Local Demo remains a separate mode and does not
authorize guest internet access.

Read-only inspection on 2026-11-26 found the VM running with one host-only NIC
(`Ethernet 2`, host address `192.168.56.1/24`), cable disconnected, no NAT or
bridged adapters, no shared folders, and clipboard, file transfer, drag-and-drop,
and USB disabled. The host-only guest-info network count is zero; the cable has
not been enabled. The Guest Additions version property currently returns no
value, so its installed version and Guest Control are not independently verified
by this check. The visible Ubuntu console showed the synthetic renderer boundary
acceptance completion, but the broker is not configured for public fetches.
The host development identity bundle is locally loadable and time-limited; its
contents were not printed. No gateway listener, firewall rule, VM cable, or public
request was started in this verification. The attached offline ISO predates the
current host fetch gateway/remote-dispatch integration; its own README describes
the earlier fixture-only package. It must be refreshed before installing this
worker version in the guest.

## Identities and protocol

The host owns plans, approvals, task state and evidence. A broker running as
`unbound-worker` verifies an Ed25519 request before dispatch. It reserves the
request ID in a SQLite transaction before performing any action. A durable wall
clock floor rejects backward jumps below the last committed authorization. This
does not detect an older VM snapshot containing both the clock and ledger. After
clock uncertainty, state loss or snapshot restore, take the worker offline,
reconcile time out of band and rotate the host authorization key before resuming.

The broker holds the private TLS and response-signing keys, proxy credential and
replay ledger. A separate `unbound-renderer` account runs Playwright and sandboxed
Chromium under systemd PrivateNetwork, AF_UNIX-only sockets and an empty capability
set. The UID and network namespace boundary must be checked on Ubuntu. Chromium
uses an ephemeral context, static fixture routing, no downloads, no JavaScript or
service workers, two sessions maximum and a 60-second idle timeout. A compromised
renderer can lie about any observation. It can also disrupt its own socket.

Each connection to `/run/unbound-renderer/control.sock` carries one big-endian
four-byte frame length and one bounded JSON message. Directory access is 0750,
socket access 0660. Both endpoints verify the other UID with Linux SO_PEERCRED.
Only the broker and renderer belong to the IPC group. The message carries task,
session and operation. Credentials, approval secrets and task mutation operations
are outside this interface. The fixture check below deliberately exercises only
this renderer boundary, independent of task approval.

The fixed worker control endpoint is `https://192.168.56.10:9443`. Only the
Windows host address .1 is accepted. TLS requires a client certificate. On the
host use a private, purpose-specific server CA, hostname checking, TLS 1.2+
and a dedicated client certificate/key. Use IP SAN 192.168.56.10 and serverAuth
on the guest certificate, clientAuth on the host certificate. For the separate
guest-to-host proxy require serverAuth with IP SAN 192.168.56.1 and pin a
dedicated CA. The worker's broker-mediated fetch mode uses the authenticated
guest-to-host fetch endpoint. The host gateway code is implemented but no TLS
listener is running in the current environment. It accepts bounded GET fetches
only; it does not support CONNECT or general-purpose tunnelling.

POST /v1/health signs a fresh challenge in each direction. POST /v1/execute
signs the protocol version, key identifier, key generation and payload.
Version 2 uses UTF-8 JSON, sorted keys, compact separators and rejects duplicate
members. Authorization binds task, step, session, tool, operation digest, approval
reference, worker identity, request ID and expiry (maximum 60 seconds). The host
pins the peer Ed25519 key. The reply includes a SHA-256 over the original signed
request. Replacing a pinned key revokes the previous signer. The peer pins exactly one public key and generation. Rotate the keys and increment
UNBOUND_HOST_SIGNING_KEY_VERSION or UNBOUND_WORKER_SIGNING_KEY_VERSION on both ends.
There is no managed certificate revocation service. Use distinct single-peer
issuers and out-of-band fingerprints. Monitor expiry of each leaf and CA, e.g.
`openssl x509 -in worker.crt -checkend 604800 -noout`. To revoke, disconnect,
stop the affected service, replace its trust anchor and pinned key, invalidate
sessions and restart only after review. Never reinstate an old ledger or key to
recover an interrupted action.

A verified signature authenticates a report. The report, its URL, text, PNG,
timestamps and success field remain guest claims. The TaskExecutor adapter binds
requests to the approved task, step, tool, exact arguments, approval, session and
request identifier; explicitly selected remote execution has no local fallback.
The host performs a separate approved-origin fetch to cross-check the reported
title and text. This does not attest the screenshot or prove that an interaction
occurred. The guest adapter and host evidence path are implemented in the working
tree, but the isolated runtime remains disabled until OS-level containment is
independently verified. Ambiguous remote outcomes are never retried automatically.

## Disconnected installation and test procedure (requires separate approval)

Prepare read-only media on a trusted machine with the reviewed checkout, locked
Linux wheels, Ubuntu-signed OS packages for Python 3.12/venv and Chromium shared
libraries, and a reviewed Playwright-compatible Linux Chromium distribution.
Do not copy host secrets or the Windows browser. The wheel lock is CPython 3.12,
Ubuntu x86-64 specific. Match wheel hashes to the release, inspect package
metadata/advisories and independently review the browser build and OS package
manifest before installation. Neither a browser distribution nor an Ubuntu OS
package lock is provided here. Keep the prior reviewed wheelhouse and manifest for
rollback. Never enable NAT to finish an offline installation.

The following commands assume a new installation, reviewed binaries under
`/opt/unbound-gate-a/chromium`, reviewed source under `/opt/unbound-gate-a`, and wheels
under `/media/unbound/wheels`. These paths must be root-owned and unwritable by
either service account. Stop on unexpected pre-existing accounts or files.

```bash
cd /opt/unbound-gate-a
sudo groupadd --system unbound-ipc
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin unbound-worker
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin unbound-renderer
sudo usermod -aG unbound-ipc unbound-worker
sudo python3.12 -m venv /opt/unbound-gate-a/.venv-worker
sudo .venv-worker/bin/python -m pip install --no-index --only-binary=:all: --require-hashes --find-links=/media/unbound/wheels -r workers/ubuntu/requirements-cp312-linux-x86_64.lock
sudo .venv-worker/bin/python -m pip check
sudo install -d -m 0700 -o root -g root /etc/unbound-browser-worker
sudo install -d -m 0700 -o unbound-worker -g unbound-worker /var/lib/unbound-browser-worker
sudo systemd-analyze verify workers/ubuntu/unbound-{browser-worker,renderer}.service
sudo install -m 0644 workers/ubuntu/unbound-{browser-worker,renderer}.service /etc/systemd/system/
sudo systemctl daemon-reload
```

For **disconnected renderer acceptance only**, provision synthetic marker files at
the three broker secret paths and a disposable replay ledger at the documented path with root ownership and mode 0600. Check absence of access
from the renderer account and inspect group membership before starting the unit.
Run the following locally on the guest console with the cable still off. No
broker HTTPS listener is required. Check the actual Chromium descendants during
the run using systemd-cgls. Abort if the Chromium sandbox cannot start.

```bash
# First-time synthetic fixtures only. Exclusive creation refuses existing files.
sudo python3 - <<'PY'
from pathlib import Path
import os
os.umask(0o077)
directory = Path("/etc/unbound-browser-worker")
for name in ("app.key", "server.key", "proxy.password"):
    with (directory / name).open("xb") as synthetic:
        synthetic.write(b"offline acceptance marker only")
PY
sudo -u unbound-worker python3 -c 'import os; os.close(os.open("/var/lib/unbound-browser-worker/replay.sqlite3", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))'
id unbound-renderer
id unbound-worker
sudo systemctl start unbound-renderer
sudo systemctl show unbound-renderer -p MainPID -p User -p Group -p PrivateNetwork -p RestrictAddressFamilies -p ControlGroup
sudo sh workers/ubuntu/check_renderer_boundary.sh
sudo systemctl stop unbound-renderer
systemctl show unbound-renderer -p MainPID -p ActiveState
```

Verify real file permissions by checking the files exist as root first; a missing
file does not prove permission denial. For each existing path below run
`sudo -u unbound-renderer test ! -r "$path"`. Test the unit credential mount,
broker process environment and ledger while the broker runs under an approved
synthetic deployment. Record actual UIDs, group lists, cgroup membership, socket
ownership, network namespace and sandbox evidence.

The broker unit uses systemd LoadCredential for root-provisioned `app.key`,
`server.cert`, `server.key`, `host-client-ca.pem`, `proxy.password` and `host.pub`
in `/etc/unbound-browser-worker`. Map the development export names as follows:
`worker-signing-key.pem` to `app.key`, `worker-server-cert.pem` to
`server.cert`, `worker-server-key.pem` to `server.key`,
`host-client-ca.pem` to `host-client-ca.pem`, `host-signing-public.pem` to
`host.pub`, and `proxy.password` unchanged. Provision the broker client
certificate/key and gateway CA separately as `broker-client-cert.pem`,
`broker-client-key.pem` and `gateway-server-ca.pem`. The unit maps the worker
TLS certificate and the host-client CA into systemd's private credential
directory; do not put their paths or private values in logs. Create the initial
empty replay.sqlite3 *only on a new guest* with exclusive
creation as unbound-worker. Missing state on an existing installation is an
incident. Keep public certificates and CA files in a separate root-owned folder
readable by the broker. A future root-owned worker.env supplies
UNBOUND_WORKER_BIND_ADDRESS=192.168.56.10, UNBOUND_WORKER_PORT=9443,
UNBOUND_WORKER_ID, UNBOUND_WORKER_PROXY_CA_FILE,
UNBOUND_WORKER_PROXY_USERNAME=unbound and
UNBOUND_WORKER_PROXY_ENDPOINT=https://192.168.56.1:18765. The certificate
and client-CA environment variables are provided by the unit from its private
credential directory. Leave
UNBOUND_BROWSER_WORKER_ENABLE unset during disconnected acceptance. The
LoadCredential paths are supplied by the unit. Validate the certificates and
unit credential modes on the actual systemd version before a broker trial.

The disconnected renderer unit defaults to the fixture when
`/etc/unbound-renderer.env` is absent. For a separately reviewed broker-fetch
trial, create a root-owned mode-0600 file containing only
`UNBOUND_BROWSER_WORKER_ENABLE=1` and `UNBOUND_RENDERER_MODE=broker_fetch`.
The renderer remains in its private network namespace and obtains page bytes
only through the broker's authenticated Unix socket. The broker unit rejects
any renderer mode other than `broker_fetch`; configuration drift fails closed.

## Network policy for administrator review

Run read-only Windows commands from PowerShell:

```powershell
$vbox = Join-Path $env:ProgramFiles 'Oracle/VirtualBox/VBoxManage.exe'
& $vbox showvminfo 'UNBOUND-Isolated-Browser' --machinereadable
Get-NetIPAddress -InterfaceAlias 'Ethernet 2'
Get-NetIPInterface -InterfaceAlias 'Ethernet 2' | Select-Object InterfaceAlias,AddressFamily,Forwarding
Get-NetFirewallProfile -PolicyStore ActiveStore | Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction
Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 18765,9443,8001,8002,5173,5175
```

There must be one guest and one host on this isolated segment. Disable clipboard
**after approval** on the running VM. These two review-only commands change its
clipboard settings. Repeat showvminfo and retain the readback before provisioning secrets.

```powershell
& $vbox controlvm 'UNBOUND-Isolated-Browser' clipboard mode disabled
& $vbox controlvm 'UNBOUND-Isolated-Browser' clipboard filetransfers off
```

Check absence of NAT, bridging, forwarding, Internet
Connection Sharing, guest DNS, default routes, shared folders and device passthrough.
Reserve 192.168.56.10/24 for the guest, with no gateway or DNS. Allow local ARP
for the two peers only. The proposed inet table does not filter or authenticate ARP;
inspect layer-2 peers and require separate link-layer controls if other machines share the segment. Additional guests,
DHCP, IPv6 router advertisements or an unknown neighbor invalidate this design.

The proposed guest policy is in `workers/ubuntu/host-only-review.nft`. Check
the console-reported adapter name with `ip -br link` and verify IPv4 and IPv6
route tables. Use `sudo nft --check --file workers/ubuntu/host-only-review.nft`
for syntax/compatibility before considering any installation. The file requires a
new named table and an isolated guest. Never flush an existing ruleset. Its input,
output and forward chains drop by default; exact control and proxy tuples and
their stateful replies are the exceptions. Systemd IPAddressAllow by itself covers
all ports on .1 and is insufficient. Renderer traffic stays entirely in its
private network namespace.

The host fetch gateway code binds only to `192.168.56.1:18765`; the existing
loopback proxy at `127.0.0.1:18765` remains separate. Before starting the listener,
review the exact host-only firewall rule, confirm the guest nftables policy and
static address, and provide the explicit DoT configuration. The gateway validates
the HTTPS allowlist, complete DoT answers, public address ranges, numeric-IP
pinning, origin TLS, redirect targets, response framing, and size/time limits. It
rejects CONNECT, upgrades, and non-GET methods. These application checks do not
prove OS-level egress containment.

The candidate narrow host inbound rule is limited to the backend Python process,
host-only interface/address, guest address, and TCP 18765. It is not installed.
This rule alone does not close other host services; review effective allows on
the adapter and other routes before activation.

```powershell
New-NetFirewallRule -DisplayName 'UNBOUND Isolated Browser Host-Only Gateway' -Direction Inbound -Action Allow -Program 'C:\Users\KARTHIK G A\UNBOUND-AI\.venv-wake\Scripts\python.exe' -Protocol TCP -LocalAddress 192.168.56.1 -LocalPort 18765 -RemoteAddress 192.168.56.10 -InterfaceAlias 'Ethernet 2' -Profile Any
```

The only required VirtualBox adapter change is enabling the already-configured
host-only NIC after guest static `192.168.56.10/24` and default-deny nftables
policy are installed and reviewed:

```powershell
& 'C:\Program Files\Oracle\VirtualBox\VBoxManage.exe' controlvm 'UNBOUND-Isolated-Browser' setlinkstate1 on
```

FastAPI/Vite stay on loopback. Permit host-initiated TCP .1 -> .10:9443 and
guest-initiated TCP .10 -> .1:18765 only. Reject all unrelated host services,
forwarding, other TCP/UDP, IPv6, DNS 53, direct DoT 853, ICMP routing and public
destinations. Apply scoped inbound deny rules or dedicated isolation if other
effective host allows cannot be ruled out. A broad Windows block plus an
overlapping allow is not a correct exception strategy.

Proxy egress remains a separate **blocking** requirement. Proposed persistent
WFP policy: dedicated protected image/App ID at ALE_AUTH_CONNECT_V4 and V6;
deny everything outside TCP to the single numeric resolver:853 and the approved
address set:443. Use disjoint veto conditions, including wrong protocol, remote
port and address. No permit overriding an existing deny, no Python exception.
Explicitly reject mapped/transition addresses and private or host-owned addresses;
constrain descendants independently. Validate object ownership, restart and
rollback with controlled listeners and OS drop/flow records. WFP alone cannot
identify the intended HTTPS site on a shared IP. The proxy also needs strict
origin TLS and DNS validation. Until these OS controls exist and are measured,
keep the listener absent. A separate locked-down egress gateway behind an
independent network ACL is an alternative if the host cannot isolate the proxy.

## Controlled acceptance and recovery

| Boundary | Procedure and required evidence |
|---|---|
| Broker/renderer | Run the disconnected fixture check; inspect actual credential access, peer UID, sandbox, no other IPC-group members, process cleanup and idle expiry. |
| Control and proxy | Observe mutual TLS, correct application signature and expected response for .1 -> .10:9443 and authenticated TLS for .10 -> .1:18765. Wrong peer, CA, password or key must fail. |
| Guest network | Use isolated synthetic TCP/UDP listeners, both IP families, before/after positive controls, per-chain counters and captures on host-only and external interfaces. Include direct 80/443, TCP/UDP 53, 853, QUIC, IPv6, non-approved ports, host 8001/8002/5173/5175, gateway/other host addresses. Model metadata with a lab-only dummy destination. Never probe real infrastructure. |
| Proxy egress | Correlate the protected PID/image to OS flow/drop events, proxy decisions and listening canaries; exercise both address families, mixed DNS, redirection, DNS outage, alternate ports, proxy shutdown, guest restart, host restart and unrelated-process controls. |
| Recovery | Use disposable state to test disk-full, corrupt ledger, concurrent writers, key changes, clock rollback and VM restore. Never recreate a lost ledger under an active authorization key. Lost replies leave outcomes uncertain; cancel/close or allow session expiry and reconcile on the host. |
| Artifacts | Record policy versions, binaries/hashes, UTC, routes, packet counters, capture digests, boot IDs, intended tuples and observed outcomes. Protect artifacts from the guest. A timeout or signed page report alone does not establish OS enforcement. |

The health response exposes A-I readiness under browser_worker.readiness_gates.
A (actual guest UID boundary), B (deployed protocol), C (deployed TLS), G
(recovery) and H (reproducible full guest install) are NOT TESTED. D (network),
E (proxy containment), F (host execution/evidence integration) and I (explicit
activation) FAIL. No gate is promoted by a health challenge. On any violation,
isolate the guest, stop only its two units, retain ledger/evidence and investigate.
An operator must authorize any later connection. No activation procedure applies
to this release.
