"""Compatibility exports for the browser-managed egress policy."""
from backend.app.egress_proxy import (
    DestinationPolicy,
    EgressDenied,
    LOCAL_DEMO_HOST,
    LOCAL_DEMO_PRODUCT_URL,
    LocalEgressProxy,
    ValidatedDestination,
    classify_address,
)

__all__ = [
    "DestinationPolicy", "EgressDenied", "LOCAL_DEMO_HOST",
    "LOCAL_DEMO_PRODUCT_URL", "LocalEgressProxy", "ValidatedDestination",
    "classify_address",
]
