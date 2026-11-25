# Windows browser containment status

## Current status

Public browser execution is disabled. The FastAPI health response reports the
managed proxy's runtime state separately from OS process containment, and the
containment field remains `false`. A healthy proxy is not evidence that
Chromium cannot connect directly.

The browser runtime uses a loopback egress proxy with per-runtime credentials.
The proxy validates complete DNS answers, rejects non-public destinations,
connects to the validated numeric address, restricts public ports to HTTP 80
and HTTPS 443, and validates each new connection. Chromium is configured to
use that proxy, disables QUIC and non-proxied WebRTC UDP, blocks service
workers, and applies request-scheme checks. These are application controls;
they do not establish a Windows process-level network boundary.

Local Demo Mode remains separate. It serves only the fixed synthetic
`unbound-demo.invalid` fixture through the managed proxy and rejects other
destinations. The public execution gate in `backend/app/api/runtime.py` must
remain closed until OS-level enforcement has been installed and independently
tested.

## Windows inspection performed

The active PowerShell process was not elevated. The supplied administrator
handoff said its PowerShell session was elevated, but that statement did not
match the process running this audit. No firewall or WFP configuration was
changed. Windows Defender Firewall profiles were enabled; their outbound
action was reported as `NotConfigured`. BFE and MpsSvc were running. No
Chromium process-specific WFP policy was independently verified.

The exact Chromium executable observed in this checkout was:

```text
C:\Users\KARTHIK G A\UNBOUND-AI\.playwright-browsers\chromium-1255\chrome-win64\chrome.exe
```

No custom WFP provider, sublayer, ALE filter installation, or rollback tool is
currently included. The Codex process is not elevated, and creating privileged
WFP policy without an elevated test environment would leave its arbitration,
loopback exception, rollback, and unrelated-application behavior unverified.
No system policy was applied and no WFP installer is supplied. Ordinary proxy
settings and URL/DNS checks must not be described as process containment.

## WFP design findings

Microsoft's user-mode WFP management API exposes engine, provider, sublayer,
filter, transaction, and deletion operations; basic application-specific ALE
filters do not inherently require a custom kernel callout. The ALE outbound
connect V4 and V6 layers expose the application ID, remote address, remote
port, and protocol conditions needed for a scoped policy. The condition list
for each filter is combined with AND semantics. WFP also evaluates all
sub-layers, and a hard block overrides a permit, so a broad deny plus an
overlapping proxy exception cannot be assumed to work. Any implementation
needs explicit non-overlapping conditions, transactional install/rollback,
and tests against the host's existing policy.

The intended Chrome rule set must be tied to the exact Chromium application
ID and the configured proxy endpoint. At ALE_AUTH_CONNECT_V4, the permitted
tuple is TCP to `127.0.0.1:18765`; every other Chromium IPv4 tuple must be
blocked. Chromium IPv6 egress must be blocked because the proxy listens only
on IPv4 loopback. The policy must also account for all Chromium subprocesses,
DNS, UDP, QUIC, and proxy-offline behavior. Those rules are a design target,
not an installed policy. Their WFP value encodings, filter weights, and
arbitration have not been validated against this host.

The standalone proxy's own ALE identity must be its exact packaged
`unbound-egress-proxy.exe`, never `python.exe`. Its network policy must be
limited to TCP HTTP/HTTPS egress and must block non-public destination ranges;
the proxy's application-level DNS/IP validation remains necessary but is not
a substitute for this OS policy. Exact WFP conditions for all non-global
IPv4/IPv6 ranges and safe transactional rollback still require implementation
and elevated testing.

The proxy now runs as a separate packaged executable. FastAPI launches it as a
child process with a distinct PID and communicates over private standard
input/output pipes. A random per-runtime proxy credential and fixed loopback
endpoint are sent at startup; the secret is not placed in command arguments,
environment variables, health responses, or audit logs. The proxy fails
startup if the configured port is occupied and never selects a fallback. The
default is `127.0.0.1:18765`; `UNBOUND_BROWSER_EGRESS_PORT` can set another
validated port for a matching policy. Local Demo Mode continues using its
exact synthetic fixture restriction.

The parent verifies the child PID, executable path, SHA-256, and a private
authenticated policy probe. While the child is running, Windows opens the
packaged executable and existing files in its `_internal` directory read-only
without write/delete sharing. This prevents replacement of those files during
that runtime. It is not a code signature, does not attest build provenance,
and does not protect a writable package directory against replacement before
startup. Deployments must protect the project-controlled directory with
suitable filesystem ACLs and build from the pinned
`requirements-browser-proxy-build.txt` dependency.

Build the executable from project-owned source with:

```powershell
.\scripts\browser-security\build-egress-proxy.ps1
```

The build uses PyInstaller one-directory mode. Its output is ignored by Git
at `backend/app/browser/bin/unbound-egress-proxy/`; a clean checkout must build
it before starting the proxy runtime. The script refuses to overwrite an
existing output. Runtime starts fail closed if the executable is missing, the
port is occupied, or the authenticated validation probe fails.

The API inventory reports backend PID/interpreter, proxy PID/path/hash, and
the installed Chromium path/hash when exactly one Playwright Chromium is
found. These are binary inventory fields, not evidence that WFP is installed
or that network containment is verified. The proxy executable hash does not
attest its `_internal` dependencies or publisher; the package directory and
build toolchain remain part of the local trust boundary. Health flags for WFP,
direct bypass, IPv4/IPv6, DNS, and UDP/QUIC verification remain false until an
external verifier supplies independent evidence; application code currently
has no such WFP verifier.

The process separation and fixed endpoint blockers are resolved in application
code, but no WFP policy currently uses the endpoint. Process-level egress
containment remains unimplemented and unverified.

## Protected browser deployment (Phase 10)

The repository now includes staged deployment tooling for a protected
installation at `C:\Program Files\UNBOUND AI`. It copies the complete
PyInstaller one-directory proxy package and the complete installed Playwright
Chromium revision directory (not only `chrome.exe`), hashes every file, writes
a deployment manifest, applies protected ACLs to each deployed file and
directory, and verifies the staged deployment before publishing it. Ordinary
Users receive read/execute only; Administrators and SYSTEM retain full
control. The verifier rejects unexpected files, reparse points, changed
hashes, non-protected ACLs, explicit deny rules, and paths outside the fixed
Program Files root. Administrators and SYSTEM remain trusted and can modify
the deployment. This does not defend against a malicious administrator or
compromise of the build toolchain.

Deployment has **not** been run in this environment: the active Codex process
is not elevated. No protected files or ACLs have been created or changed. The
runtime supports explicit `UNBOUND_BROWSER_PROTECTED_MODE=1`; in that mode it
verifies the manifest, complete file inventory, executable hashes, and ACLs
before using the deployed proxy and setting `PLAYWRIGHT_BROWSERS_PATH`. Any
failure disables browser integration without falling back to checkout or
user-writable binaries. Development mode remains the default and is reported
separately. Neither mode changes the public browsing gate.

After review, an administrator may deploy and verify the binaries with:

```powershell
Set-Location 'C:\Users\KARTHIK G A\UNBOUND-AI'
.\scripts\browser-security\deploy-protected-browser.ps1
.\scripts\browser-security\verify-protected-browser.ps1
```

Set `UNBOUND_BROWSER_PROTECTED_MODE=1` for the backend only after the verifier
reports success. The deployment is removed only by the ownership- and
manifest-checking script, with the deployment ID confirmed from verification:

```powershell
.\scripts\browser-security\remove-protected-browser.ps1 -ExpectedDeploymentId '<verified-deployment-id>'
```

These deployment scripts are not WFP setup scripts and do not establish
network containment.

## Phase 10 WFP implementation status

No WFP provider, sublayer, filter, or install/remove tool has been created.
No Windows policy was installed. The active Codex process is not elevated, and
the available environment has neither a .NET SDK nor the Windows SDK C++
compiler. More importantly, the current proxy resolves names through the
Windows resolver; an application-ID ALE rule for the proxy does not by itself
constrain DNS Client service traffic to that proxy identity. A correct policy
therefore needs a separately designed/controlled resolver path (or another
verified DNS mediation mechanism) in addition to app-ID filters. Implementing
and publishing an untested filter set from this process would risk claiming a
false boundary or interfering with unrelated networking. No WFP commands
should be run and public browsing must remain disabled until that design is
implemented and reviewed, then installed and independently tested.

For eventual WFP tooling, the design must bind to the protected executable
identities, use transactional ownership-scoped objects, and avoid relying on
an overlapping allow overriding a hard block. At ALE connect layers, filter
conditions are ANDed and hard blocks can win across sublayers. The Chromium
policy must permit only TCP to `127.0.0.1:18765` and block every other IPv4
tuple plus all IPv6 egress; the proxy policy must separately constrain TCP
ports and non-global destinations while handling DNS through an identity-bound
resolver design. These are requirements, not installed filters or verified
behavior.

WFP offers dynamic sessions whose objects are removed when their owning
process exits, and persistent objects that remain until deleted. Dynamic
cleanup alone would be unsafe here if the machine's effective outbound policy
allows traffic after the session disappears. A production setup therefore
needs a deliberately chosen persistent-policy lifecycle, atomic transactions,
an exact rollback, and restart/crash tests. None of these installation or
enforcement tests has been performed in this environment.

References: [filtering conditions by layer](https://learn.microsoft.com/en-us/windows/win32/fwp/filtering-conditions-available-at-each-filtering-layer),
[filter arbitration](https://learn.microsoft.com/en-us/windows/win32/fwp/filter-arbitration),
[WFP object lifetime and transactions](https://learn.microsoft.com/en-us/windows/win32/fwp/object-management),
and [user-mode WFP management functions](https://learn.microsoft.com/en-us/windows-hardware/drivers/network/wfp-user-mode-management-functions).

## Required administrator work before public browsing

Use a separate elevated PowerShell session to design and validate an
application-specific Windows Filtering Platform policy for the exact
Chromium executable. The policy must cover IPv4 and IPv6 at outbound connect
authorization, permit Chromium to reach only the configured loopback proxy
endpoint, and deny its other TCP and UDP/DNS egress. It must not affect Python,
the backend, other Chrome installations, or unrelated applications. The
policy must have a unique UNBOUND identity and a rollback that removes only
UNBOUND-created objects.

Before any installation, verify the target executable and elevation:

```powershell
$chrome = 'C:\Users\KARTHIK G A\UNBOUND-AI\.playwright-browsers\chromium-1255\chrome-win64\chrome.exe'
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator
)
Get-Item -LiteralPath $chrome | Select-Object FullName, Length, LastWriteTimeUtc
Get-FileHash -LiteralPath $chrome -Algorithm SHA256
Get-Service BFE, MpsSvc | Select-Object Name, Status, StartType
Get-NetFirewallProfile | Select-Object Name, Enabled, DefaultOutboundAction
```

Then implement and independently review separate idempotent install, verify,
and rollback operations against Microsoft's WFP management API and ALE
connect-authorization layers. Do not apply a policy until its exact filters,
loopback exception, IPv4/IPv6 coverage, process identity, and rollback have
been reviewed. Keep the public gate disabled while any part remains unproven.

## Required verification before changing the gate

Use controlled test endpoints only. From the actual Chromium process, verify
that the proxy endpoint remains reachable, while direct TCP, UDP, DNS, IPv4,
IPv6, QUIC/WebRTC, redirects, subresources, popups, and proxy shutdown cannot
create a bypass. Verify the proxy's public egress policy separately. Confirm
the policy is still enforced after backend restart and that removing the
UNBOUND policy restores only the prior state. Do not probe cloud metadata or
internal infrastructure. A successful Local Demo or proxy test is not proof
of public-network containment.

Only after those checks pass should a reviewed code change expose public
execution as enabled. Until then, the accurate status is: Local Demo may be
available; public browsing is disabled; process containment is unverified.
