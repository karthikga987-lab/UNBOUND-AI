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
currently included. Creating privileged WFP policy without an elevated test
environment would leave its filtering and rollback behavior unverified, so no
installer is supplied or run. Ordinary proxy settings and URL/DNS checks must
not be described as process containment.

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

The current proxy is not a distinct OS process: `TaskRuntime` constructs
`LocalEgressProxy` in the FastAPI process. A policy that allowed the Python
executable to make proxy egress would also grant that same executable's other
code the same identity. This does not meet the requested proxy identity
boundary. Before a policy can be designed for both processes, the proxy must
be isolated into a dedicated executable or restricted service identity, or a
separately reviewed enforcement mechanism must constrain its destinations.
The default proxy port is ephemeral (`UNBOUND_BROWSER_EGRESS_PORT=0`); a
fixed, reserved loopback port must be configured for any exact WFP exception.
Runtime support for that setting is present, but no firewall policy currently
uses it.

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
