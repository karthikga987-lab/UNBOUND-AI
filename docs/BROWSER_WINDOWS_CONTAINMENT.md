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
