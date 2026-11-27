"""Synthetic control-plane tests. TLS travels through memory BIOs; no IP sockets."""
from datetime import datetime, timedelta, timezone
import ipaddress
import os
import ssl
import sqlite3
import stat
import struct
from io import BytesIO
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from backend.app.browser import worker_protocol as protocol
from backend.app.browser.guest_proxy_policy import ApprovedDestinationPolicy, ApprovedOrigin
from backend.app.browser.worker_service import ReplayGuard, worker_tls_context
from backend.app.browser.worker_ipc import RenderRequest, frame
from backend.app.browser.worker_renderer import FixtureRenderer
from backend.app.egress_proxy import EgressDenied
from pathlib import Path


def test_replay_after_prune_and_clock_rollback(tmp_path):
    db = tmp_path / 'ledger'
    first = uuid4()
    ReplayGuard(db).consume(first, 111, now=100)
    ReplayGuard(db).consume(uuid4(), 220, now=200)
    with pytest.raises(protocol.WorkerProtocolError, match="clock"):
        ReplayGuard(db).consume(first, 111, now=105)


def test_signed_header_duplicate_keys_and_unsigned_envelope():
    key = ed25519.Ed25519PrivateKey.generate()
    body = protocol.sign_payload({'event': 'health'}, key)
    for candidate in (body.replace(b'"event":', b'"event":"health","event":'),
                      body.replace(b'worker/v2', b'worker/v1'), b'{}',
                      body.replace(b'"health"', b'"ready"')):
        with pytest.raises(protocol.WorkerProtocolError):
            protocol.verify_envelope(candidate, key.public_key(), max_bytes=4096)


def test_renderer_rejects_internet_before_launch_and_reaps_idle_session(monkeypatch):
    worker = FixtureRenderer()
    job = RenderRequest(task_id=uuid4(), session_id=uuid4(), operation={"operation":"navigate","url":"https://example.com/"})
    monkeypatch.setattr(worker, "_create_session", lambda: pytest.fail("browser launch"))
    assert worker.execute(job).failure_code == "invalid_destination"
    key = (job.task_id, job.session_id)
    worker._last_used[key] = -100
    called = []
    monkeypatch.setattr(worker, "_close", called.append)
    worker.expire_idle()
    assert called == [key]


def test_renderer_readiness_launches_and_cleans_browser_without_open_session(monkeypatch):
    from types import SimpleNamespace

    renderer = FixtureRenderer()
    closed = []
    stopped = []
    browser = SimpleNamespace(is_connected=lambda: True, close=lambda: closed.append(True))
    playwright = SimpleNamespace(stop=lambda: stopped.append(True))
    monkeypatch.setattr(renderer, "_create_session", lambda _key: (playwright, browser, object()))

    assert renderer.browser_ready() is True
    assert renderer._sessions == {}
    assert closed == [True]
    assert stopped == [True]

    unavailable = FixtureRenderer()
    monkeypatch.setattr(unavailable, "_create_session", lambda _key: (_ for _ in ()).throw(OSError()))
    assert unavailable.browser_ready() is False


def test_ipc_refuses_secrets_and_trust_metadata():
    for extra in ("proxy_password", "verified", "approval_id", "command"):
        with pytest.raises(ValueError):
            RenderRequest(task_id=uuid4(), session_id=uuid4(), operation={"operation":"close"}, **{extra: "x"})


@pytest.mark.parametrize("wire", [struct.pack("!I", 2048), struct.pack("!I", 9) + b'abc', b''])
def test_frame_rejects_oversize_and_truncation(wire):
    class Stream:
        recv = BytesIO(wire).read
        def settimeout(self, _):
            pass
    import time
    with pytest.raises(ValueError):
        frame(Stream(), 1024, time.monotonic() + 3)


def test_guest_profile_intersects_policy_and_complete_answers():
    ip = "93.184.216.34"
    answers = [ip]
    policy = ApprovedDestinationPolicy((ApprovedOrigin("example.com", frozenset([ip])),),
                                      resolver=lambda *_: answers)
    assert str(policy.validate("example.com", 443).addresses[0]) == ip
    for host, port in [(ip, 443), ("other.example", 443), ("example.com", 80)]:
        with pytest.raises(EgressDenied):
            policy.validate(host, port)
    for changed in ("127.0.0.1", "2001:4860:4860::8888"):
        answers[:] = [ip, changed]
        with pytest.raises(EgressDenied):
            policy.validate("example.com", 443)


def make_ca(name):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1)))
    return key, builder.add_extension(x509.BasicConstraints(ca=True, path_length=0), True).sign(key, hashes.SHA256())


def leaf(tmp, ca, name, usage, expired=False):
    issuer, cert = ca
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    leaf_cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
                 .issuer_name(cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
                 .not_valid_before(now-timedelta(days=2)).not_valid_after(now+timedelta(days=-1 if expired else 1))
                 .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("192.168.56.10"))]), False)
                 .add_extension(x509.ExtendedKeyUsage([usage]), False).sign(issuer, hashes.SHA256()))
    c, k = tmp / (name+'.crt'), tmp / (name+'.key')
    c.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(c), str(k)


def handshake(client, server, target):
    ci, co, si, so = (ssl.MemoryBIO() for _ in range(4))
    c = client.wrap_bio(ci, co, server_hostname=target)
    s = server.wrap_bio(si, so, server_side=True)
    done = set()
    for _ in range(20):
        for peer, outgoing, incoming in ((c, co, si), (s, so, ci)):
            try:
                peer.do_handshake()
                done.add(peer)
            except ssl.SSLWantReadError:
                pass
            incoming.write(outgoing.read())
        if len(done) == 2:
            return
    pytest.fail("handshake did not terminate")


@pytest.mark.parametrize("case", ["valid", "wrong-san", "wrong-ca", "missing-client", "expired"])
def test_actual_mutual_tls_in_memory(tmp_path, case):
    ca = make_ca("test CA")
    ca_path = tmp_path / "ca.crt"
    ca_path.write_bytes(ca[1].public_bytes(serialization.Encoding.PEM))
    server = worker_tls_context(*leaf(tmp_path, ca, "server", ExtendedKeyUsageOID.SERVER_AUTH, case=="expired"), str(ca_path))
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    trust = make_ca("other CA") if case == "wrong-ca" else ca
    client.load_verify_locations(cadata=trust[1].public_bytes(serialization.Encoding.PEM).decode())
    if case != "missing-client":
        client.load_cert_chain(*leaf(tmp_path, ca, "client", ExtendedKeyUsageOID.CLIENT_AUTH))
    if case == "valid":
        client = protocol.HttpsWorkerTransport.from_identity_files(
            "https://192.168.56.10:9443", guest_ca=str(ca_path),
            client_cert=str(tmp_path / "client.crt"), client_key=str(tmp_path / "client.key"),
        ).ssl_context
        handshake(client, server, "192.168.56.10")
    else:
        with pytest.raises(ssl.SSLError):
            handshake(client, server, "192.168.56.11" if case == "wrong-san" else "192.168.56.10")

def test_replay_storage_failure_refuses_execution(tmp_path):
    db = tmp_path / "ledger"
    ledger = ReplayGuard(db)
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TRIGGER storage_failure BEFORE INSERT ON consumed_requests
                        BEGIN SELECT RAISE(FAIL, 'synthetic write refused'); END""")
    with pytest.raises(protocol.WorkerProtocolError):
        ledger.consume(uuid4(), 201, now=200)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM clock_floor").fetchone()[0] == 0
    corrupt = tmp_path / "corrupt"
    corrupt.write_bytes(b"synthetic non-database" * 6)
    with pytest.raises(sqlite3.DatabaseError):
        ReplayGuard(corrupt)


def test_envelope_rejects_type_changed_header():
    key = ed25519.Ed25519PrivateKey.generate()
    msg = protocol.sign_payload({"check": "bound"}, key)
    for value in (b"true", b"1.0", b'"1"'):
        with pytest.raises(protocol.WorkerProtocolError):
            protocol.verify_envelope(msg.replace(b'"key_version":1', b'"key_version":'+value),
                                     key.public_key(), max_bytes=4096)


def test_wrong_ipc_peer_is_denied(monkeypatch):
    import socket
    from backend.app.browser.worker_ipc import verify_peer
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    class Peer:
        def getsockopt(self, *args):
            return struct.pack("3i", 100, 999, 456)
    with pytest.raises(PermissionError):
        verify_peer(Peer(), 250)


def test_rotated_generation_rejects_previous_signature():
    key = ed25519.Ed25519PrivateKey.generate()
    old = protocol.sign_payload({"synthetic": True}, key, key_version=3)
    with pytest.raises(protocol.WorkerProtocolError):
        protocol.verify_envelope(old, key.public_key(), max_bytes=4096, key_version=4)
    fresh = protocol.sign_payload({"synthetic": True}, key, key_version=4)
    assert protocol.verify_envelope(fresh, key.public_key(), max_bytes=4096, key_version=4)["synthetic"]


def test_networked_worker_requires_broker_fetch_mode(monkeypatch):
    from backend.app.browser import worker_service

    monkeypatch.setenv("UNBOUND_BROWSER_WORKER_ENABLE", "1")
    monkeypatch.setenv("UNBOUND_RENDERER_MODE", "fixture")
    with pytest.raises(SystemExit, match="broker_fetch"):
        worker_service.main()


def test_public_worker_trust_key_allows_read_only_systemd_credential_mode(tmp_path, monkeypatch):
    from backend.app.browser.worker_service import _read_worker_file

    key_file = tmp_path / "host.pub"
    key_file.write_bytes(b"synthetic-public-key")
    key_file.chmod(0o444)
    monkeypatch.setenv("TEST_WORKER_PUBLIC_KEY", str(key_file))

    assert _read_worker_file("TEST_WORKER_PUBLIC_KEY", public_material=True) == b"synthetic-public-key"
    with pytest.raises(SystemExit, match="secret file permissions are too broad"):
        _read_worker_file("TEST_WORKER_PUBLIC_KEY")

    key_file.chmod(0o666)
    with pytest.raises(SystemExit, match="trust file permissions are unsafe"):
        _read_worker_file("TEST_WORKER_PUBLIC_KEY", public_material=True)


@pytest.mark.skipif(os.name != "posix", reason="systemd credential directories are POSIX-only")
def test_worker_secret_allows_only_private_systemd_credential_directory(tmp_path, monkeypatch):
    from backend.app.browser.worker_service import _read_worker_file

    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    credentials.chmod(0o700)
    key_file = credentials / "app.key"
    key_file.write_bytes(b"synthetic-secret")
    key_file.chmod(0o444)
    monkeypatch.setenv("TEST_WORKER_SECRET", str(key_file))
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))

    assert _read_worker_file("TEST_WORKER_SECRET") == b"synthetic-secret"

    monkeypatch.delenv("CREDENTIALS_DIRECTORY")
    with pytest.raises(SystemExit, match="secret file permissions are too broad"):
        _read_worker_file("TEST_WORKER_SECRET")

    credentials.chmod(0o755)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    with pytest.raises(SystemExit, match="secret file permissions are too broad"):
        _read_worker_file("TEST_WORKER_SECRET")


def test_worker_secret_accepts_systemd_root_owned_credential_acl_and_rejects_world_readable(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from backend.app.browser import worker_service

    credentials_root = tmp_path / "run" / "credentials"
    credentials = credentials_root / "unbound-browser-worker.service"
    credentials.mkdir(parents=True)
    key_file = credentials / "app.key"
    key_file.write_bytes(b"synthetic-systemd-secret")
    monkeypatch.setattr(worker_service, "SYSTEMD_CREDENTIALS_ROOT", credentials_root)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    monkeypatch.setenv("TEST_WORKER_SYSTEMD_SECRET", str(key_file))

    original_lstat = type(key_file).lstat
    metadata = {
        credentials_root: SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0),
        credentials: SimpleNamespace(st_mode=stat.S_IFDIR | 0o550, st_uid=0, st_gid=0),
        key_file: SimpleNamespace(st_mode=stat.S_IFREG | 0o440, st_uid=0, st_gid=0),
    }

    def lstat_with_systemd_ownership(path):
        return metadata.get(path, original_lstat(path))

    monkeypatch.setattr(type(key_file), "lstat", lstat_with_systemd_ownership)
    monkeypatch.setattr(worker_service.os, "access", lambda *args, **kwargs: True)
    assert worker_service._read_worker_file("TEST_WORKER_SYSTEMD_SECRET") == b"synthetic-systemd-secret"

    metadata[key_file] = SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_uid=0, st_gid=0)
    with pytest.raises(SystemExit, match="secret file permissions are too broad"):
        worker_service._read_worker_file("TEST_WORKER_SYSTEMD_SECRET")


def test_networked_renderer_requires_broker_fetch_mode(monkeypatch):
    from backend.app.browser.worker_ipc import _configured_renderer_mode

    monkeypatch.setenv("UNBOUND_BROWSER_WORKER_ENABLE", "1")
    monkeypatch.setenv("UNBOUND_RENDERER_MODE", "fixture")
    with pytest.raises(RuntimeError, match="broker_fetch"):
        _configured_renderer_mode()
    monkeypatch.setenv("UNBOUND_RENDERER_MODE", "broker_fetch")
    assert _configured_renderer_mode() == "broker_fetch"


def test_systemd_worker_units_select_broker_fetch_without_network_fallback():
    root = Path(__file__).resolve().parents[2]
    worker = (root / "workers/ubuntu/unbound-browser-worker.service").read_text()
    renderer = (root / "workers/ubuntu/unbound-renderer.service").read_text()
    packager = (root / "workers/ubuntu/package-gate-a.ps1").read_text()
    assert "Environment=UNBOUND_RENDERER_MODE=broker_fetch" in worker
    assert "Environment=UNBOUND_WORKER_PROXY_USERNAME=unbound" in worker
    assert "Environment=UNBOUND_WORKER_PROXY_ENDPOINT=https://192.168.56.1:18765" in worker
    assert "LoadCredential=server.cert:/etc/unbound-browser-worker/server.cert" in worker
    assert "LoadCredential=host-client-ca.pem:/etc/unbound-browser-worker/host-client-ca.pem" in worker
    assert "Environment=UNBOUND_WORKER_TLS_CERT_FILE=%d/server.cert" in worker
    assert "Environment=UNBOUND_WORKER_CLIENT_CA_FILE=%d/host-client-ca.pem" in worker
    assert "backend/app/browser/worker_fetch.py" in packager
    assert "workers/ubuntu/host-only-review.nft" in packager
    assert "EnvironmentFile=-/etc/unbound-renderer.env" in renderer
    assert "PrivateNetwork=true" in renderer
    assert "IPAddressDeny=any" in renderer
    assert "RestrictAddressFamilies=AF_UNIX" in renderer
    assert "NoNewPrivileges=true" in renderer
    assert "CapabilityBoundingSet=" in renderer
    assert "PrivateNetwork=true" not in worker
    assert "IPAddressDeny=any" in worker
    assert "IPAddressAllow=192.168.56.1/32" in worker
    assert "RestrictAddressFamilies=AF_UNIX AF_INET" in worker


def test_guest_network_policy_is_default_deny_and_gateway_only_template():
    root = Path(__file__).resolve().parents[2]
    policy = (root / "workers/ubuntu/host-only-review.nft").read_text()

    # This file is a review template only; it is not evidence that nftables was
    # installed or that these rules are active in the guest.
    assert "No installer applies this file" in policy
    assert policy.count("policy drop;") == 3
    assert 'iifname $dev ip saddr $host ip daddr $guest tcp dport 9443' in policy
    assert 'oifname $dev ip saddr $guest ip daddr $host tcp dport 18765' in policy
    assert "tcp dport 53" not in policy
    assert "udp dport 53" not in policy
    assert "tcp dport 80" not in policy
    assert "tcp dport 443" not in policy
    assert "masquerade" not in policy.casefold()
