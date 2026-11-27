# Host gateway containment preparation

The staged `unbound-fetch-gateway.exe` is a separate program identity from
FastAPI's `python.exe`. Its complete PyInstaller directory must be deployed
under `C:\Program Files\UNBOUND AI\gateway\` with ordinary users limited to
read/execute. Record and verify the executable SHA-256 and all bundled files
before starting it. Setting `UNBOUND_FETCH_GATEWAY_MODE=external` makes FastAPI
skip its in-process listener while keeping TaskExecutor dispatch disabled. Do
not start both gateway modes on `192.168.56.1:18765`.

The intended outbound policy is application-specific at
`ALE_AUTH_CONNECT_V4` and `ALE_AUTH_CONNECT_V6`, matching the exact protected
gateway executable. It must allow TCP/853 only to the configured numeric
DNS-over-TLS resolver and TCP/443 only to the exact IP addresses returned by
the gateway's complete-answer validation for approved hosts. Validated IP
permissions must expire with the DNS answer. Every other IPv4/IPv6 destination,
UDP packet, DNS port 53 request, and protocol must be denied. The gateway's
existing exact-host HTTPS, TLS certificate/hostname, redirect, and pinned-IP
checks remain required.

No Windows outbound policy is installed by this preparation. Windows Defender
Firewall currently has a default outbound action of Allow. A broad program
block combined with narrow allow exceptions is **not** a safe substitute:
WFP block arbitration can override the permit. A reviewed, privileged WFP
policy manager and independent direct-connect tests are required before the
host egress gate can pass. The existing inbound host-only rule is unrelated to
gateway outbound containment.

On Ubuntu, `unbound-guest-network-policy.service` is installed and enabled. It
waits for the host-only address, checks that no default route exists, and either
loads the dedicated `inet unbound_guest` table or accepts an existing table
only when its normalized rules exactly match the reviewed policy. It leaves the
default-drop policy installed when stopped. The worker requires this unit.

Live guest checks after a clean reboot confirmed the policy unit active before
the worker was enabled, with no default route. Renderer namespace probes to
synthetic IPv4, IPv6, TCP DNS and UDP DNS destinations failed. These checks do
not verify host gateway egress containment; Windows Gate E remains blocked.

Public execution remains disabled until both OS policies and the full worker
dispatch/evidence path are independently verified.

## Post-reboot verification record — 2026-11-26

### Gate D — PASS for the observed Ubuntu boot

The following checks were run against `UNBOUND-Isolated-Browser` after its
latest boot, from the guest console. The guest clock reported
`2026-11-26T20:11:57+05:30` during the check session; later console captures
showed 20:13–20:17 IST. The console only displayed minute precision in those
later captures, so per-probe seconds were not recorded.

VirtualBox inspection confirmed UUID
`c8846b0f-1564-481d-ab48-179dcf9d3d77`, one connected host-only adapter, and
NICs 2–8 set to `none` (no NAT or bridged adapter). The guest had only the
`192.168.56.0/24` host-only route and link-local IPv6 route; neither guest nor
renderer had a default route.

Guest commands and sanitized results:

```text
date -Is
2026-11-26T20:11:57+05:30

sudo -n true
exit status 0

systemctl is-active unbound-guest-network-policy unbound-browser-worker unbound-renderer
active
active
active

systemctl is-enabled unbound-guest-network-policy unbound-browser-worker unbound-renderer
enabled
enabled
static

systemctl show unbound-renderer -p PrivateNetwork -p RestrictAddressFamilies \
  -p IPAddressDeny -p CapabilityBoundingSet -p AmbientCapabilities
PrivateNetwork=yes
RestrictAddressFamilies=AF_UNIX
IPAddressDeny=0.0.0.0/0 ::/0
CapabilityBoundingSet=
AmbientCapabilities=

sudo /bin/sh /opt/unbound-gate-a/workers/ubuntu/install-host-only-policy.sh
existing UNBOUND nft table matches the reviewed host-only policy
exit status 0
```

The current-boot journal showed the policy unit starting at
`2026-11-26 18:08:33 IST`, before the renderer and worker start events at about
`19:48 IST`. The worker was enabled and active after boot. Before the restart
test its cgroup contained only the worker service process, with no Chromium
task running.

Renderer probes ran inside the renderer service's network namespace using
`nsenter -t "$(systemctl show -p MainPID --value unbound-renderer)" -n`. The
namespace exposed only `lo`; both route tables were empty. Connection attempts
to the host gateway (`192.168.56.1:18765`), TEST-NET IPv4 HTTPS and DNS targets,
and documentation-prefix IPv6 HTTPS and DNS targets all failed with Linux
`errno 101` (`ENETUNREACH`). UDP DNS probes to synthetic IPv4 and IPv6 targets
also failed with `errno 101`. No public destination or DNS resolver was
contacted by these probes.

The worker was restarted with:

```bash
sudo systemctl restart unbound-browser-worker.service
sudo /bin/sh /opt/unbound-gate-a/workers/ubuntu/install-host-only-policy.sh
systemctl is-active unbound-guest-network-policy.service \
  unbound-renderer.service unbound-browser-worker.service
```

The installer again reported `existing UNBOUND nft table matches the reviewed
host-only policy` and returned 0; all three units reported `active`. This
verifies that a worker restart did not remove or weaken the default-drop table.

The installer refusal paths were also exercised in a mocked WSL Ubuntu test
(not an OS-level guest test):

```powershell
wsl.exe --distribution Ubuntu-24.04 --exec bash -lc `
  "bash '/mnt/c/Users/KARTHIK G A/UNBOUND-AI/workers/ubuntu/test-host-only-policy-guards.sh'"
```

Result:

```text
unexpected IPv4 default route: rejected before nftables access
unexpected IPv6 default route: rejected before nftables access
altered existing nftables table: rejected without replacement
exit status 0
```

A new host-side signed/mTLS health challenge was made after the guest boot and
worker restart. The guest health handler checks `GatewayClient.proxy_available`
before reporting ready, so this also caused the broker to perform a fresh mTLS
health request to the host gateway. At
`2026-11-26T20:27:24.6753416+05:30` through
`2026-11-26T20:27:25.0868317+05:30`, the sanitized result was:

The host-side command, run from the repository root, was:

```powershell
$identity = Get-ChildItem -LiteralPath (Join-Path $env:LOCALAPPDATA 'UNBOUND\worker-dev') -Directory |
  Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
$env:UNBOUND_WORKER_ID = 'unbound-ubuntu-worker'
$env:UNBOUND_WORKER_ENDPOINT = 'https://192.168.56.10:9443'
& '.\.venv-wake\Scripts\python.exe' -c "import sys; from backend.app.browser.worker_runtime import build_worker_client; h=build_worker_client(sys.argv[1]).health(); print({'mTLS_health':'PASS','worker_id':h.worker_id,'ready':h.ready,'browser_available':h.browser_available,'proxy_configured':h.proxy_configured})" $identity.FullName
```

```text
mTLS_health=PASS
worker_id=unbound-ubuntu-worker
ready=True
browser_available=True
proxy_configured=True
```

The VM console subsequently locked due to inactivity. A later queued,
non-secret shell command was interpreted by the login screen as a failed
password attempt; no credential was entered, exposed, or recorded, and no
further guest keystrokes were sent. The authenticated services remained
reachable for the fresh health exchange.

This Gate D result is a manually executed acceptance record for the observed
boot. At `2026-11-26T20:32:10+05:30`, FastAPI `/api/health` still reported
`browser_public_execution_enabled=False`, `execution_enabled=False`,
`containment_verified=False`, D=`FAIL`, E=`FAIL`, and
`fetch_gateway_active=True`; its recorded worker health timestamp was
`2026-11-26T18:17:15+05:30`. The endpoint does not import the manual Gate D
record or the later independent mTLS probe. This is conservative for
execution, but the displayed D/health timestamp is stale and should not be
read as the result of this manual check. No public browsing was enabled.

### Gate E — PARTIAL; outbound containment is not installed

Windows Firewall profiles were enabled and their active outbound action was
`Allow` for Domain, Private, and Public. The existing rule named `UNBOUND
Isolated Browser Gateway` is **inbound only** and scoped to the host-only
gateway listener; it does not constrain gateway outbound traffic. The live
host gateway listener was in the same `python.exe` process as FastAPI (PID
32472), so an application-scoped policy on that executable would also affect
FastAPI. The separate gateway build remains only in a user-writable temporary
staging directory, has SHA-256
`BCD126B7C817C6EBCA1C9045B49822D8CE5209DA69C4EB19D96955E336CED629`, and is
not Authenticode-signed. It has not been deployed or started.

No WFP provider, sublayer, filter, or privileged policy manager was installed
by this task; WFP state was not independently enumerated. No UNBOUND outbound
firewall rule was found. No allowed/denied OS-level gateway egress tests,
restart-persistence tests, or unrelated-process comparison were performed.
The current process is not elevated. Gate E therefore remains **PARTIAL**;
this is not only an elevation blocker—the dynamic per-destination policy
manager and protected standalone deployment still need implementation and
review.

Do not use a broad Windows Firewall block rule plus allow exceptions as a
substitute. WFP arbitration is layer- and sublayer-based; a hard block can
override a permit. See Microsoft's [Filter Arbitration documentation](https://learn.microsoft.com/en-us/windows/win32/fwp/filter-arbitration).
Any future policy must use a reviewed application-specific design, preserve
the gateway's exact-host/DNS/TLS checks, and be tested against the effective
policy before public execution can be enabled.

The existing loopback proxy PID 2968, host-only adapter, host firewall rules,
system networking, and Windows security settings were not changed during this
verification. The public browsing gate remains disabled.

### Follow-up: API guest-policy attestation gate

The host protocol now accepts Gate D only when an authenticated worker health
response contains a fresh attestation bound to the current request ID, expected
worker identity, boot ID, reviewed policy version, and SHA-256 of
`workers/ubuntu/host-only-review.nft`. Every required policy, renderer, route,
and bypass check must be exactly true. `/api/health` refreshes the signed mTLS
health challenge (cached for at most three seconds) and reports D as `PASS`
only for a verified attestation. This does not change Gate E, enable dispatch,
or enable public browsing.

After that host-side change, a fresh mTLS health request to the currently
installed Ubuntu worker returned `ready=True` but no `guest_policy_attestation`;
the verified Gate D result was therefore `guest_policy_attestation_missing`.
The attestation collector/provider is not yet installed or connected in the
guest service, and the already-running FastAPI process was left untouched so
this source change is not loaded into that process. Do not interpret a mocked
attestation test as live Gate D evidence. Deploy a root-owned, read-only guest
policy verifier and have the broker include its current result in the signed
health response before D can pass in the API.

Host-side regression verification after the protocol change:

```text
backend/tests/test_browser_worker.py
backend/tests/test_browser_runtime_integration.py
backend/tests/test_api.py
84 passed, 1 Starlette deprecation warning

backend/tests (full backend suite)
746 passed, 3 skipped, 1 Starlette deprecation warning

frontend npm test
85 passed (14 files)
frontend npm run lint
passed
frontend npm run build -- --outDir <unique temporary directory> --emptyOutDir
passed
```

These are application tests/builds. They do not establish Windows WFP
enforcement or enable public website access.
