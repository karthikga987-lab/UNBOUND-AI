from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.app.browser.state_encryption import (
    ACTIVE_KEY_ENV,
    PREVIOUS_KEYS_ENV,
    BrowserReplayStateCipher,
    ReplayStateDecryptionError,
    ReplayStateEncryptionConfigurationError,
)


def _key_setting(key_id: str, key: bytes) -> str:
    encoded = base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")
    return f"{key_id}:{encoded}"


def _configure(monkeypatch, key_id="test-v1", key=None):
    monkeypatch.setenv(ACTIVE_KEY_ENV, _key_setting(key_id, key or os.urandom(32)))
    monkeypatch.delenv(PREVIOUS_KEYS_ENV, raising=False)


def _envelope(data: bytes) -> dict:
    return json.loads(data.decode("utf-8"))


def test_encrypt_decrypt_round_trip_and_ciphertext_hides_plaintext(monkeypatch):
    _configure(monkeypatch)
    cipher = BrowserReplayStateCipher()
    secret = b"replay password parameter value"

    encrypted = cipher.encrypt(secret)

    assert cipher.decrypt(encrypted) == secret
    assert secret not in encrypted


def test_each_encryption_uses_a_unique_nonce(monkeypatch):
    _configure(monkeypatch)
    cipher = BrowserReplayStateCipher()

    nonces = {_envelope(cipher.encrypt(b"same value"))["nonce"] for _ in range(64)}

    assert len(nonces) == 64


def test_tampered_ciphertext_fails_with_safe_message(monkeypatch):
    _configure(monkeypatch)
    cipher = BrowserReplayStateCipher()
    envelope = _envelope(cipher.encrypt(b"private value"))
    ciphertext = envelope["ciphertext"]
    changed = "A" if ciphertext[0] != "A" else "B"
    envelope["ciphertext"] = changed + ciphertext[1:]

    with pytest.raises(ReplayStateDecryptionError, match="could not be authenticated") as error:
        cipher.decrypt(json.dumps(envelope).encode())
    assert "private value" not in str(error.value)


def test_tampered_nonce_fails_authentication_without_disclosing_data(monkeypatch):
    key = b"n" * 32
    _configure(monkeypatch, key=key)
    cipher = BrowserReplayStateCipher()
    envelope = _envelope(cipher.encrypt(b"nonce-bound private value"))
    nonce = base64.urlsafe_b64decode(envelope["nonce"] + "=" * (-len(envelope["nonce"]) % 4))
    changed_nonce = bytes([nonce[0] ^ 1]) + nonce[1:]
    envelope["nonce"] = base64.urlsafe_b64encode(changed_nonce).decode("ascii").rstrip("=")
    ciphertext = envelope["ciphertext"]
    encoded_key = _key_setting("test-v1", key).split(":", 1)[1]

    with pytest.raises(ReplayStateDecryptionError, match="could not be authenticated") as error:
        cipher.decrypt(json.dumps(envelope).encode())
    assert "nonce-bound private value" not in str(error.value)
    assert ciphertext not in str(error.value)
    assert encoded_key not in str(error.value)


def test_tampered_configured_key_id_fails_authentication_without_disclosure(monkeypatch):
    # Same key bytes under two configured IDs isolate key-ID AAD authentication.
    key = b"m" * 32
    monkeypatch.setenv(ACTIVE_KEY_ENV, _key_setting("active-v2", key))
    encoded_key = base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")
    monkeypatch.setenv(PREVIOUS_KEYS_ENV, json.dumps({"previous-v1": encoded_key}))
    cipher = BrowserReplayStateCipher()
    envelope = _envelope(cipher.encrypt(b"key-id-bound private value"))
    envelope["key_id"] = "previous-v1"
    ciphertext = envelope["ciphertext"]

    with pytest.raises(ReplayStateDecryptionError, match="could not be authenticated") as error:
        cipher.decrypt(json.dumps(envelope).encode())
    assert "key-id-bound private value" not in str(error.value)
    assert ciphertext not in str(error.value)
    assert encoded_key not in str(error.value)


def test_wrong_key_fails_closed_without_disclosing_data(monkeypatch):
    _configure(monkeypatch, key=b"a" * 32)
    encrypted = BrowserReplayStateCipher().encrypt(b"secret payload")
    _configure(monkeypatch, key=b"b" * 32)

    with pytest.raises(ReplayStateDecryptionError, match="could not be authenticated") as error:
        BrowserReplayStateCipher().decrypt(encrypted)
    assert "secret payload" not in str(error.value)


def test_missing_and_malformed_keys_fail_closed(monkeypatch):
    monkeypatch.delenv(ACTIVE_KEY_ENV, raising=False)
    monkeypatch.delenv(PREVIOUS_KEYS_ENV, raising=False)
    with pytest.raises(ReplayStateEncryptionConfigurationError, match="must be configured"):
        BrowserReplayStateCipher()

    monkeypatch.setenv(ACTIVE_KEY_ENV, "test-v1:not-a-valid-key")
    with pytest.raises(ReplayStateEncryptionConfigurationError, match="key"):
        BrowserReplayStateCipher()

    monkeypatch.setenv(ACTIVE_KEY_ENV, _key_setting("test-v1", b"short"))
    with pytest.raises(ReplayStateEncryptionConfigurationError, match="32 bytes"):
        BrowserReplayStateCipher()


def test_duplicate_previous_key_ids_fail_closed_without_echoing_key_material(monkeypatch):
    active_key = _key_setting("v2", b"a" * 32)
    duplicate_value = base64.urlsafe_b64encode(b"b" * 32).decode("ascii").rstrip("=")
    monkeypatch.setenv(ACTIVE_KEY_ENV, active_key)
    monkeypatch.setenv(
        PREVIOUS_KEYS_ENV,
        f'{{"v1":"{duplicate_value}","v1":"{duplicate_value}"}}',
    )

    with pytest.raises(ReplayStateEncryptionConfigurationError, match="JSON object") as error:
        BrowserReplayStateCipher()
    assert duplicate_value not in str(error.value)
    assert "v1" not in str(error.value)


def test_rotation_decrypts_previous_key_and_encrypts_with_active_key(monkeypatch):
    previous_key = b"p" * 32
    active_key = b"n" * 32
    _configure(monkeypatch, "v1", previous_key)
    old_envelope = BrowserReplayStateCipher().encrypt(b"persisted replay state")

    monkeypatch.setenv(ACTIVE_KEY_ENV, _key_setting("v2", active_key))
    previous_encoded = base64.urlsafe_b64encode(previous_key).decode("ascii").rstrip("=")
    monkeypatch.setenv(PREVIOUS_KEYS_ENV, json.dumps({"v1": previous_encoded}))
    rotated = BrowserReplayStateCipher()

    assert rotated.decrypt(old_envelope) == b"persisted replay state"
    assert _envelope(rotated.encrypt(b"new state"))["key_id"] == "v2"


def test_unsupported_format_and_unconfigured_key_versions_fail_closed(monkeypatch):
    _configure(monkeypatch)
    cipher = BrowserReplayStateCipher()
    envelope = _envelope(cipher.encrypt(b"state"))
    envelope["format_version"] = 2
    with pytest.raises(ReplayStateDecryptionError, match="unsupported format version"):
        cipher.decrypt(json.dumps(envelope).encode())

    envelope = _envelope(cipher.encrypt(b"state"))
    envelope["key_id"] = "retired-v0"
    with pytest.raises(ReplayStateDecryptionError, match="key version is unavailable"):
        cipher.decrypt(json.dumps(envelope).encode())


def test_encrypted_state_can_be_decrypted_by_a_new_process(monkeypatch, tmp_path):
    key = os.urandom(32)
    _configure(monkeypatch, "restart-v1", key)
    ciphertext_path = Path(tmp_path) / "state.enc"
    ciphertext_path.write_bytes(BrowserReplayStateCipher().encrypt(b"restart-safe state"))
    environment = os.environ.copy()
    script = (
        "from pathlib import Path; "
        "from backend.app.browser.state_encryption import BrowserReplayStateCipher; "
        "print(BrowserReplayStateCipher().decrypt(Path(r'"
        + str(ciphertext_path)
        + "').read_bytes()).decode())"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "restart-safe state"
