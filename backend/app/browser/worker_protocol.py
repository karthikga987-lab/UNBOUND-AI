"""Strict signed protocol for a task-authorized remote browser worker.

The worker receives one host-signed operation at a time. Responses contain raw
observations only; task/evidence identifiers and verification remain owned by
the Windows orchestrator.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import re
import secrets
import ssl
import time
import urllib.error
import urllib.request
from typing import Annotated, Any, Literal, Protocol, Union
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator


MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_TTL_SECONDS = 60
MAX_CLOCK_SKEW_SECONDS = 15
MAX_SCREENSHOT_BYTES = 2 * 1024 * 1024
WORKER_HOST_ONLY_NETWORK = "192.168.56.0/24"
GUEST_POLICY_VERSION = "unbound-guest-host-only-v1"
# SHA-256 of workers/ubuntu/host-only-review.nft (the host-only policy source).
# Change this constant together with a reviewed policy revision.
GUEST_POLICY_SHA256 = "3c8a034b5710c4de7619af54def90a3b90e42fac0b76405e4165387e7802afc9"
GUEST_POLICY_ATTESTATION_MAX_AGE_SECONDS = 30


class WorkerProtocolError(ValueError):
    """Safe protocol failure with a stable machine-readable category."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Locator(StrictModel):
    type: Literal["role", "label", "placeholder", "text", "test_id", "css"]
    value: str = Field(min_length=1, max_length=512)
    name: str | None = Field(default=None, max_length=512)
    exact: bool = False


class Navigate(StrictModel):
    operation: Literal["navigate"]
    url: str = Field(min_length=1, max_length=4096)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValueError("browser URL contains control characters")
        try:
            parts = urlsplit(value)
            _ = parts.port
        except ValueError:
            raise ValueError("invalid browser URL") from None
        if value == "http://unbound-demo.invalid/product":
            return value
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError("only absolute HTTP(S) URLs are allowed")
        if parts.username is not None or parts.password is not None:
            raise ValueError("credentials in browser URLs are not allowed")
        if parts.port not in {None, 80, 443} or (parts.scheme.lower() == "http" and parts.port == 443) or (parts.scheme.lower() == "https" and parts.port == 80):
            raise ValueError("browser destination port is not allowed")
        host = parts.hostname.casefold()
        if (value != value.strip() or "\\" in value or not host.isascii()
                or not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}", host)
                or host.endswith((".localhost", ".local", ".internal", ".invalid", ".home.arpa"))
                or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+)){0,3}", host)):
            raise ValueError("browser destination must be an unambiguous DNS name")
        return value


class Click(StrictModel):
    operation: Literal["click"]
    target: Locator


class TypeText(StrictModel):
    operation: Literal["type"]
    target: Locator
    text: str = Field(max_length=2048)


class Scroll(StrictModel):
    operation: Literal["scroll"]
    delta_x: int = Field(default=0, ge=-2000, le=2000)
    delta_y: int = Field(ge=-2000, le=2000)


class Screenshot(StrictModel):
    operation: Literal["screenshot"]


class ExtractVisible(StrictModel):
    operation: Literal["extract_visible"]
    max_chars: int = Field(default=12000, ge=1, le=20000)


class WaitNavigation(StrictModel):
    operation: Literal["wait_navigation"]
    state: Literal["domcontentloaded", "load", "networkidle"] = "domcontentloaded"
    timeout_ms: int = Field(default=10000, ge=100, le=15000)


class CloseBrowser(StrictModel):
    operation: Literal["close"]


BrowserOperation = Annotated[
    Union[Navigate, Click, TypeText, Scroll, Screenshot, ExtractVisible, WaitNavigation, CloseBrowser],
    Field(discriminator="operation"),
]
_OPERATION_ADAPTER = TypeAdapter(BrowserOperation)


class WorkerRequest(StrictModel):
    worker_id: str = Field(min_length=1, max_length=128)
    approval_id: UUID
    tool_name: Literal["browser.navigate", "browser.click", "browser.fill"]
    request_id: UUID
    task_id: UUID
    step_id: str = Field(min_length=1, max_length=128)
    session_id: UUID
    issued_at: int
    expires_at: int
    operation: BrowserOperation
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_time_and_digest(self) -> "WorkerRequest":
        ttl = self.expires_at - self.issued_at
        if ttl <= 0 or ttl > MAX_REQUEST_TTL_SECONDS:
            raise ValueError("request authorization lifetime is invalid")
        primary_tool = {"navigate": "browser.navigate", "click": "browser.click", "type": "browser.fill"}.get(self.operation.operation)
        if primary_tool and self.tool_name != primary_tool:
            raise ValueError("operation does not match authorized tool")
        expected = operation_digest(self.operation)
        if self.arguments_sha256 != expected:
            raise ValueError("operation authorization digest does not match arguments")
        return self


class WorkerResult(StrictModel):
    success: bool
    current_url: str | None = Field(default=None, max_length=4096)
    title: str | None = Field(default=None, max_length=1024)
    visible_text: str | None = Field(default=None, max_length=20000)
    visible_text_truncated: bool = False
    screenshot_base64: str | None = Field(default=None, max_length=MAX_SCREENSHOT_BYTES * 2)
    content_trust: Literal["untrusted_webpage_data"] = "untrusted_webpage_data"
    failure_code: Literal[
        "worker_operation_failed", "browser_unavailable", "invalid_destination",
        "proxy_unavailable", "invalid_session", "timeout",
    ] | None = None

    @field_validator("screenshot_base64")
    @classmethod
    def check_screenshot(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("screenshot is not valid base64") from None
        if not raw or len(raw) > MAX_SCREENSHOT_BYTES:
            raise ValueError("screenshot size is invalid")
        return value

    @model_validator(mode="after")
    def no_success_with_failure(self) -> "WorkerResult":
        if self.success and self.failure_code is not None:
            raise ValueError("successful worker result cannot carry a failure code")
        if not self.success and self.failure_code is None:
            raise ValueError("failed worker result requires a failure code")
        return self


class WorkerResponse(StrictModel):
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: UUID
    task_id: UUID
    step_id: str
    session_id: UUID
    worker_id: str = Field(min_length=1, max_length=128)
    completed_at: int
    result: WorkerResult


class HealthChallenge(StrictModel):
    request_id: UUID
    challenge: str = Field(min_length=32, max_length=128)
    issued_at: int


class GuestPolicyChecks(StrictModel):
    policy_unit_active: bool
    nft_table_matches_reviewed_policy: bool
    default_routes_absent: bool
    policy_starts_before_worker: bool
    worker_and_renderer_active: bool
    renderer_private_network: bool
    renderer_unix_socket_only: bool
    renderer_ip_deny_all: bool
    renderer_separate_network_namespace: bool
    renderer_direct_ipv4_blocked: bool
    renderer_direct_ipv6_blocked: bool
    renderer_tcp_dns_blocked: bool
    renderer_udp_dns_blocked: bool


class GuestPolicyAttestation(StrictModel):
    """Challenge-bound guest observations signed inside the worker envelope."""

    request_id: UUID
    worker_id: str = Field(min_length=1, max_length=128)
    boot_id: UUID
    policy_version: Literal["unbound-guest-host-only-v1"]
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: int = Field(strict=True, ge=0)
    checks: GuestPolicyChecks


class WorkerHealth(StrictModel):
    request_id: UUID
    challenge: str
    worker_id: str
    ready: bool
    browser_available: bool
    proxy_configured: bool
    guest_policy_attestation: GuestPolicyAttestation | None = None
    public_browsing_enabled: Literal[False] = False


def verify_guest_policy_attestation(
    health: WorkerHealth,
    *,
    expected_worker_id: str,
    now: int | None = None,
    max_age_seconds: int = GUEST_POLICY_ATTESTATION_MAX_AGE_SECONDS,
) -> tuple[bool, str]:
    """Accept only a fresh, signed-health-bound report for the reviewed policy.

    The caller must first authenticate the health envelope and challenge with
    ``BrowserWorkerClient.health``. Missing evidence is deliberately a failure.
    """
    current = int(time.time()) if now is None else now
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 60:
        return False, "invalid_attestation_window"
    evidence = getattr(health, "guest_policy_attestation", None)
    if evidence is None:
        return False, "guest_policy_attestation_missing"
    if evidence.request_id != health.request_id or evidence.worker_id != expected_worker_id or health.worker_id != expected_worker_id:
        return False, "guest_policy_attestation_identity_mismatch"
    if evidence.policy_version != GUEST_POLICY_VERSION or evidence.policy_sha256 != GUEST_POLICY_SHA256:
        return False, "guest_policy_version_mismatch"
    age = current - evidence.observed_at
    if age < -5 or age > max_age_seconds:
        return False, "guest_policy_attestation_stale"
    if any(value is not True for value in evidence.checks.model_dump().values()):
        return False, "guest_policy_check_failed"
    return True, "verified"


class SignedEnvelope(StrictModel):
    protocol: Literal["unbound-worker/v2"]
    key_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    key_version: int = Field(strict=True, ge=1, le=2147483647)
    payload: dict[str, Any]
    signature: str = Field(min_length=80, max_length=128)


class WorkerTransport(Protocol):
    def request(self, method: str, path: str, body: bytes, timeout: float) -> tuple[int, bytes]: ...


class HttpsWorkerTransport:
    """Small HTTPS transport with caller-supplied CA/mTLS context and no redirects."""

    def __init__(self, endpoint: str, ssl_context):
        self.endpoint = validate_worker_endpoint(endpoint)
        if ssl_context.verify_mode != ssl.CERT_REQUIRED or not ssl_context.check_hostname:
            raise ValueError("worker TLS context must verify certificates and hostnames")
        self.ssl_context = ssl_context

    @classmethod
    def from_identity_files(cls, endpoint: str, *, guest_ca: str, client_cert: str, client_key: str):
        """Trust the provisioned guest issuer only; present the host transport identity."""
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_verify_locations(cafile=guest_ca)
        tls.load_cert_chain(certfile=client_cert, keyfile=client_key)
        return cls(endpoint, tls)

    def request(self, method: str, path: str, body: bytes, timeout: float) -> tuple[int, bytes]:
        if method != "POST" or path not in {"/v1/health", "/v1/execute"}:
            raise WorkerProtocolError("invalid_request", "worker transport path is not allowed")
        limit = 4096 if path.endswith("health") else MAX_REQUEST_BYTES
        if len(body) > limit:
            raise WorkerProtocolError("request_too_large", "worker request exceeds the allowed size")
        request = urllib.request.Request(
            self.endpoint + path, data=body, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.ssl_context), NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise WorkerProtocolError("response_too_large", "worker response exceeds the allowed size")
                return response.status, data
        except urllib.error.HTTPError as exc:
            body = exc.read(1024)
            return exc.code, body


def operation_digest(operation: BrowserOperation | dict[str, Any]) -> str:
    payload = operation.model_dump(mode="json") if isinstance(operation, BaseModel) else operation
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def private_key_from_pem(value: bytes) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(value, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise WorkerProtocolError("invalid_key", "configured private key must be Ed25519")
    return key


def public_key_from_pem(value: bytes) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(value)
    if not isinstance(key, Ed25519PublicKey):
        raise WorkerProtocolError("invalid_key", "configured public key must be Ed25519")
    return key


def checked_key_version(version: int) -> int:
    if type(version) is not int or not 1 <= version <= 2147483647:
        raise ValueError("invalid expected signing key version")
    return version


def sign_payload(payload: BaseModel | dict[str, Any], key: Ed25519PrivateKey, *, key_version: int = 1) -> bytes:
    value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    message = {"protocol": "unbound-worker/v2", "key_version": checked_key_version(key_version),
               "key_id": key_fingerprint(key.public_key()), "payload": value}
    signature = base64.urlsafe_b64encode(key.sign(canonical_json(message))).decode("ascii")
    return canonical_json({**message, "signature": signature})


def key_fingerprint(key):
    return hashlib.sha256(key.public_bytes_raw()).hexdigest()


def unique_members(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON member")
        result[name] = value
    return result


def verify_envelope(data: bytes, key: Ed25519PublicKey, *, max_bytes: int, key_version: int = 1) -> dict[str, Any]:
    if not isinstance(data, bytes) or len(data) > max_bytes:
        raise WorkerProtocolError("request_too_large", "worker message exceeds the allowed size")
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=unique_members)
        envelope = SignedEnvelope.model_validate(raw, strict=True)
        if type(raw["key_version"]) is not int:
            raise ValueError("invalid signing key version")
        signature = base64.b64decode(envelope.signature.encode("ascii"), altchars=b"-_", validate=True)
        if envelope.key_id != key_fingerprint(key) or envelope.key_version != checked_key_version(key_version):
            raise InvalidSignature()
        key.verify(signature, canonical_json({k: v for k, v in raw.items() if k != "signature"}))
    except InvalidSignature:
        raise WorkerProtocolError("authentication_failed", "worker message signature is invalid") from None
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, json.JSONDecodeError, binascii.Error):
        raise WorkerProtocolError("invalid_message", "worker message is malformed") from None
    return envelope.payload


def make_worker_request(
    *, task_id: UUID, step_id: str, session_id: UUID, operation: BrowserOperation,
    worker_id: str, approval_id: UUID, tool_name: str,
    now: int | None = None, ttl_seconds: int = 30,
) -> WorkerRequest:
    issued = int(time.time()) if now is None else now
    return WorkerRequest(
        request_id=uuid4(), task_id=task_id, step_id=step_id, session_id=session_id,
        worker_id=worker_id, approval_id=approval_id, tool_name=tool_name,
        issued_at=issued, expires_at=issued + ttl_seconds, operation=operation,
        arguments_sha256=operation_digest(operation),
    )


def validate_worker_endpoint(endpoint: str) -> str:
    """Require HTTPS to a literal address in the configured host-only subnet."""
    try:
        parts = urlsplit(endpoint)
        address = ipaddress.ip_address(parts.hostname or "")
        network = ipaddress.ip_network(WORKER_HOST_ONLY_NETWORK, strict=True)
        port = parts.port
    except ValueError:
        raise WorkerProtocolError("invalid_endpoint", "worker endpoint must use a literal host-only IP") from None
    if (
        parts.scheme != "https" or address not in network or not address.is_private
        or address in {network.network_address, network.broadcast_address}
        or address == network.network_address + 1
        or parts.username is not None or parts.password is not None
        or parts.path not in {"", "/"} or parts.query or parts.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise WorkerProtocolError("invalid_endpoint", "worker endpoint is outside the HTTPS host-only policy")
    if endpoint not in {"https://192.168.56.10:9443", "https://192.168.56.10:9443/"}:
        raise WorkerProtocolError("invalid_endpoint", "worker endpoint must match the reserved guest and port")
    return "https://192.168.56.10:9443"


@dataclass(frozen=True)
class AuthenticatedObservation:
    """Host-owned receipt. Raw signed bytes are preserved for subsequent review."""
    receipt_id: UUID
    authorization: WorkerRequest
    request_sha256: str
    worker_key_id: str
    received_at: int
    response_sha256: str
    signed_response: bytes
    response: WorkerResponse

    @property
    def independently_verified(self) -> bool:
        return False


class BrowserWorkerClient:
    """HTTPS-only signed client. It never opens a listener or falls back locally."""

    def __init__(
        self, *, endpoint: str, host_signing_key: Ed25519PrivateKey,
        worker_verification_key: Ed25519PublicKey, worker_id: str,
        transport: WorkerTransport, enabled: bool = False,
        timeout_seconds: float = 10.0, host_key_version: int = 1, worker_key_version: int = 1,
    ) -> None:
        self.endpoint = validate_worker_endpoint(endpoint)
        if not 0.1 <= timeout_seconds <= 60:
            raise ValueError("worker timeout must be between 0.1 and 60 seconds")
        if not isinstance(enabled, bool):
            raise ValueError("worker enabled flag must be boolean")
        self.host_key_version = checked_key_version(host_key_version)
        self.worker_key_version = checked_key_version(worker_key_version)
        self.host_signing_key = host_signing_key
        self.worker_verification_key = worker_verification_key
        self.worker_id = worker_id
        self.transport = transport
        self.enabled = bool(enabled)
        self.timeout_seconds = timeout_seconds

    def health(self) -> WorkerHealth:
        if not self.enabled:
            raise WorkerProtocolError("worker_disabled", "remote browser worker is disabled")
        challenge = HealthChallenge(request_id=uuid4(), challenge=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"), issued_at=int(time.time()))
        try:
            status, data = self.transport.request("POST", "/v1/health", sign_payload(challenge, self.host_signing_key, key_version=self.host_key_version), self.timeout_seconds)
        except TimeoutError:
            raise WorkerProtocolError("worker_timeout", "remote browser worker timed out") from None
        except OSError:
            raise WorkerProtocolError("worker_unavailable", "remote browser worker is unavailable") from None
        if status != 200:
            raise WorkerProtocolError("worker_unavailable", "remote worker health check failed")
        payload = verify_envelope(data, self.worker_verification_key, max_bytes=MAX_RESPONSE_BYTES, key_version=self.worker_key_version)
        try:
            result = WorkerHealth.model_validate_json(canonical_json(payload), strict=True)
        except ValidationError:
            raise WorkerProtocolError("invalid_response", "remote worker health response is malformed") from None
        if result.request_id != challenge.request_id or result.challenge != challenge.challenge or result.worker_id != self.worker_id:
            raise WorkerProtocolError("worker_authentication_failed", "remote worker identity or challenge did not match")
        return result

    def execute(self, request: WorkerRequest) -> WorkerResponse:
        return self.observe(request).response

    def observe(self, request: WorkerRequest) -> AuthenticatedObservation:
        if not self.enabled:
            raise WorkerProtocolError("worker_disabled", "remote browser worker is disabled")
        now = int(time.time())
        if request.issued_at > now + MAX_CLOCK_SKEW_SECONDS or request.expires_at <= now:
            raise WorkerProtocolError("request_expired", "browser worker authorization is expired")
        if self.worker_id != request.worker_id:
            raise WorkerProtocolError("wrong_worker", "request has another recipient")
        body = sign_payload(request, self.host_signing_key, key_version=self.host_key_version)
        if len(body) > MAX_REQUEST_BYTES:
            raise WorkerProtocolError("request_too_large", "browser worker request exceeds the allowed size")
        try:
            status, data = self.transport.request("POST", "/v1/execute", body, self.timeout_seconds)
        except TimeoutError:
            raise WorkerProtocolError("worker_timeout", "remote browser worker timed out") from None
        except OSError:
            raise WorkerProtocolError("worker_unavailable", "remote browser worker is unavailable") from None
        if status != 200:
            raise WorkerProtocolError("worker_rejected", "remote browser worker rejected the request")
        payload = verify_envelope(data, self.worker_verification_key, max_bytes=MAX_RESPONSE_BYTES, key_version=self.worker_key_version)
        try:
            response = WorkerResponse.model_validate_json(canonical_json(payload), strict=True)
        except ValidationError:
            raise WorkerProtocolError("invalid_response", "remote browser response is malformed") from None
        if (
            response.request_sha256 != hashlib.sha256(body).hexdigest() or response.request_id != request.request_id or response.task_id != request.task_id
            or response.step_id != request.step_id or response.session_id != request.session_id
            or response.worker_id != self.worker_id
        ):
            raise WorkerProtocolError("response_mismatch", "remote browser response did not match its authorization")
        if int(time.time()) >= request.expires_at:
            raise WorkerProtocolError("response_expired", "worker authorization expired in transit")
        if (
            response.completed_at < request.issued_at - MAX_CLOCK_SKEW_SECONDS
            or response.completed_at > request.expires_at + MAX_CLOCK_SKEW_SECONDS
        ):
            raise WorkerProtocolError("response_expired", "remote browser response timestamp is outside its authorization window")
        if response.result.success:
            report = response.result
            operation = request.operation
            if isinstance(operation, Navigate) and report.current_url != operation.url:
                raise WorkerProtocolError("invalid_response", "navigation result has no matching source")
            if isinstance(operation, Screenshot) and (
                not report.screenshot_base64 or not base64.b64decode(report.screenshot_base64).startswith(b"\x89PNG\r\n\x1a\n")
            ):
                raise WorkerProtocolError("invalid_response", "missing PNG observation")
            if isinstance(operation, ExtractVisible) and (
                report.visible_text is None or len(report.visible_text) > operation.max_chars
            ):
                raise WorkerProtocolError("invalid_response", "missing or oversized page observation")
            if isinstance(operation, (ExtractVisible, Screenshot)) and not report.current_url:
                raise WorkerProtocolError("invalid_response", "missing observation source")
        return AuthenticatedObservation(
            receipt_id=uuid4(), authorization=request, request_sha256=hashlib.sha256(body).hexdigest(),
            worker_key_id=key_fingerprint(self.worker_verification_key), received_at=int(time.time()),
            response_sha256=hashlib.sha256(data).hexdigest(), signed_response=data, response=response,
        )
