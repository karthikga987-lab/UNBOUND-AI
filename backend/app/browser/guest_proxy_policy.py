"""Offline composition for the future dedicated guest proxy profile.

No listener or entry point. The packaged loopback proxy uses its existing policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address

from backend.app.egress_proxy import DestinationPolicy, EgressDenied, _valid_dns_hostname, classify_address


@dataclass(frozen=True)
class ApprovedOrigin:
    hostname: str
    addresses: frozenset[str]

    def __post_init__(self):
        if not _valid_dns_hostname(self.hostname) or self.hostname != self.hostname.lower():
            raise ValueError("expected canonical public DNS name")
        if not self.addresses or not all(classify_address(ip) and str(ip_address(ip)) == ip for ip in self.addresses):
            raise ValueError("expected canonical public addresses")


class ApprovedDestinationPolicy(DestinationPolicy):
    """Intersect complete DoT answers with the administrator-approved IP set.

    The inherited connect() consumes the validated numeric addresses and checks
    the connected peer. A changed CDN address requires an explicit policy update.
    """

    def __init__(self, origins: tuple[ApprovedOrigin, ...], **kwargs):
        super().__init__(**kwargs)
        self._approved = {item.hostname: item.addresses for item in origins}
        if not self._approved or len(self._approved) != len(origins):
            raise ValueError("explicit unique approved origins required")

    def validate(self, hostname, port):
        if hostname not in self._approved or port != 443:
            raise EgressDenied("Destination outside approved HTTPS origins")
        result = super().validate(hostname, port)
        if any(str(ip) not in self._approved[hostname] for ip in result.addresses):
            raise EgressDenied("DNS answer outside approved address set")
        return result
