"""Create genuine seven-day development identities in an empty private directory.

No listener is started and no trust is installed. The caller must restrict the
destination's Windows ACL before invocation. Guest keys are exported encrypted;
host signing/client keys never enter the guest export.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def generate(destination: Path) -> dict:
    if not destination.is_dir() or destination.is_symlink() or any(destination.iterdir()):
        raise ValueError("An existing empty private destination is required")
    if os.name != "nt" and destination.stat().st_mode & 0o077:
        raise ValueError("Destination permissions must be 0700")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=7)
    material: dict[str, bytes] = {}

    def private(key):
        return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())

    def pair(prefix, common_name, *, address=None, client=False):
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "UNBOUND development " + prefix)])
        ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
              .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=5)).not_valid_after(expires)
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
              .sign(ca_key, hashes.SHA256()))
        leaf_key = ec.generate_private_key(ec.SECP256R1())
        leaf = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
                .issuer_name(ca.subject).public_key(leaf_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(expires)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH]), critical=False))
        if address:
            leaf = leaf.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]), critical=False)
        material[prefix + "-ca.pem"] = ca.public_bytes(serialization.Encoding.PEM)
        material[prefix + "-cert.pem"] = leaf.sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
        material[prefix + "-key.pem"] = private(leaf_key)
        # CA private keys are deliberately not retained; re-provision to rotate.

    pair("worker-server", "unbound-ubuntu-worker", address="192.168.56.10")
    pair("host-client", "unbound-host-control", client=True)
    pair("gateway-server", "unbound-host-fetch", address="192.168.56.1")
    pair("broker-client", "unbound-broker-fetch", client=True)
    for name in ("host-signing", "worker-signing"):
        key = ed25519.Ed25519PrivateKey.generate()
        material[name + "-key.pem"] = private(key)
        material[name + "-public.pem"] = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    material["proxy.password"] = secrets.token_urlsafe(48).encode("ascii")
    guest_names = {
        "worker-server-cert.pem", "worker-server-key.pem", "host-client-ca.pem",
        "gateway-server-ca.pem", "broker-client-cert.pem", "broker-client-key.pem",
        "host-signing-public.pem", "worker-signing-key.pem", "proxy.password",
    }
    guest_payload = json.dumps({name: base64.b64encode(material[name]).decode("ascii")
                                for name in sorted(guest_names)}, sort_keys=True).encode()
    transfer_key = AESGCM.generate_key(bit_length=256)
    nonce = secrets.token_bytes(12)
    material["guest-identities.enc"] = nonce + AESGCM(transfer_key).encrypt(nonce, guest_payload, b"unbound-guest-dev-v1")
    material["transfer.key"] = base64.b64encode(transfer_key)
    for name, data in material.items():
        fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    manifest = {
        "purpose": "development only", "created_at": now.isoformat(), "expires_at": expires.isoformat(),
        "guest_export": "guest-identities.enc", "guest_files": sorted(guest_names),
        "public_fingerprints": {name: hashlib.sha256(data).hexdigest() for name, data in material.items()
                                if name.endswith(("-ca.pem", "-cert.pem", "-public.pem"))},
        "encrypted_export_sha256": hashlib.sha256(material["guest-identities.enc"]).hexdigest(),
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = generate(args.output)
    print(json.dumps({"status": "created", "expires_at": result["expires_at"],
                      "encrypted_export_sha256": result["encrypted_export_sha256"]}))
