"""Versioned AES-256-GCM encryption for persisted browser replay state.

Active key configuration uses ``BROWSER_REPLAY_STATE_KEY`` in the form
``<key-id>:<base64url-encoded-32-byte-key>``. Previous keys may be supplied as
a JSON object in ``BROWSER_REPLAY_STATE_PREVIOUS_KEYS``. This module never
creates or persists keys and never falls back to plaintext.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
from typing import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ACTIVE_KEY_ENV = "BROWSER_REPLAY_STATE_KEY"
PREVIOUS_KEYS_ENV = "BROWSER_REPLAY_STATE_PREVIOUS_KEYS"
ENVELOPE_VERSION = 1
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_NONCE_BYTES = 12
_KEY_BYTES = 32
_AAD_PREFIX = b"UNBOUND-AI:browser-replay-state:"


class ReplayStateEncryptionError(RuntimeError):
    """Base error for replay-state encryption configuration and payloads."""


class ReplayStateEncryptionConfigurationError(ReplayStateEncryptionError):
    """Raised when key configuration is missing or malformed."""


class ReplayStateDecryptionError(ReplayStateEncryptionError):
    """Raised when an envelope cannot safely be decrypted."""


def _decode_key(encoded: object) -> bytes:
    if not isinstance(encoded, str) or not encoded or not _B64URL.fullmatch(encoded):
        raise ReplayStateEncryptionConfigurationError("Replay-state encryption key is invalid")
    unpadded = encoded.rstrip("=")
    try:
        key = base64.b64decode(
            unpadded + "=" * (-len(unpadded) % 4), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError):
        raise ReplayStateEncryptionConfigurationError(
            "Replay-state encryption key is invalid"
        ) from None
    if len(key) != _KEY_BYTES or base64.urlsafe_b64encode(key).decode("ascii").rstrip("=") != unpadded:
        raise ReplayStateEncryptionConfigurationError(
            "Replay-state encryption key must encode exactly 32 bytes"
        )
    return key


def _decode_envelope_field(value: object) -> bytes:
    if not isinstance(value, str) or not value or not _B64URL.fullmatch(value):
        raise ReplayStateDecryptionError("Encrypted replay state is invalid")
    unpadded = value.rstrip("=")
    try:
        decoded = base64.b64decode(
            unpadded + "=" * (-len(unpadded) % 4), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError):
        raise ReplayStateDecryptionError("Encrypted replay state is invalid") from None
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != unpadded:
        raise ReplayStateDecryptionError("Encrypted replay state is invalid")
    return decoded


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while rejecting duplicate keys instead of shadowing."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration key")
        result[key] = value
    return result


class BrowserReplayStateCipher:
    """AES-256-GCM encryptor using explicit active and rotation keys.

    The active environment variable has the format ``key-id:base64url-key``.
    Previous keys are a JSON mapping from key ID to base64url key. Previous
    keys are decryption-only; new envelopes always use the active key.
    """

    def __init__(self) -> None:
        active_setting = os.environ.get(ACTIVE_KEY_ENV)
        if not active_setting:
            raise ReplayStateEncryptionConfigurationError(
                f"{ACTIVE_KEY_ENV} must be configured"
            )

        key_id, separator, encoded_key = active_setting.partition(":")
        if not separator or not _KEY_ID.fullmatch(key_id):
            raise ReplayStateEncryptionConfigurationError(
                f"{ACTIVE_KEY_ENV} must contain a valid key ID and encoded key"
            )
        active_key = _decode_key(encoded_key)
        keys = {key_id: active_key}

        previous_setting = os.environ.get(PREVIOUS_KEYS_ENV, "")
        if previous_setting:
            try:
                previous_values = json.loads(
                    previous_setting,
                    object_pairs_hook=_unique_json_object,
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                raise ReplayStateEncryptionConfigurationError(
                    f"{PREVIOUS_KEYS_ENV} must be a JSON object"
                ) from None
            if not isinstance(previous_values, dict):
                raise ReplayStateEncryptionConfigurationError(
                    f"{PREVIOUS_KEYS_ENV} must be a JSON object"
                )
            for previous_id, previous_encoded in previous_values.items():
                if (
                    not isinstance(previous_id, str)
                    or not _KEY_ID.fullmatch(previous_id)
                    or previous_id == key_id
                ):
                    raise ReplayStateEncryptionConfigurationError(
                        "Replay-state key IDs are invalid or duplicated"
                    )
                keys[previous_id] = _decode_key(previous_encoded)

        self._active_key_id = key_id
        self._keys = keys

    @property
    def active_key_id(self) -> str:
        """Return the non-secret active key version identifier."""
        return self._active_key_id

    def encrypt(self, plaintext: bytes) -> bytes:
        """Return a JSON envelope containing authenticated ciphertext only."""
        if not isinstance(plaintext, bytes):
            raise TypeError("Replay-state plaintext must be bytes")

        # Generate a fresh 96-bit nonce for every AES-GCM encryption operation.
        nonce = secrets.token_bytes(_NONCE_BYTES)
        aad = self._associated_data(ENVELOPE_VERSION, self._active_key_id)
        ciphertext = AESGCM(self._keys[self._active_key_id]).encrypt(nonce, plaintext, aad)
        envelope = {
            "format_version": ENVELOPE_VERSION,
            "key_id": self._active_key_id,
            "nonce": self._encode_bytes(nonce),
            "ciphertext": self._encode_bytes(ciphertext),
        }
        return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def decrypt(self, envelope_bytes: bytes) -> bytes:
        """Authenticate and decrypt an envelope or fail closed with safe errors."""
        if not isinstance(envelope_bytes, bytes):
            raise ReplayStateDecryptionError("Encrypted replay state is invalid")
        try:
            envelope = json.loads(envelope_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            raise ReplayStateDecryptionError("Encrypted replay state is invalid") from None
        if not isinstance(envelope, dict):
            raise ReplayStateDecryptionError("Encrypted replay state is invalid")
        format_version = envelope.get("format_version")
        if type(format_version) is not int or format_version != ENVELOPE_VERSION:
            raise ReplayStateDecryptionError("Encrypted replay state uses an unsupported format version")
        if set(envelope) != {"format_version", "key_id", "nonce", "ciphertext"}:
            raise ReplayStateDecryptionError("Encrypted replay state is invalid")

        key_id = envelope.get("key_id")
        if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
            raise ReplayStateDecryptionError("Encrypted replay state is invalid")
        key = self._keys.get(key_id)
        if key is None:
            raise ReplayStateDecryptionError("Encrypted replay state key version is unavailable")
        nonce = _decode_envelope_field(envelope.get("nonce"))
        ciphertext = _decode_envelope_field(envelope.get("ciphertext"))
        if len(nonce) != _NONCE_BYTES:
            raise ReplayStateDecryptionError("Encrypted replay state is invalid")

        try:
            return AESGCM(key).decrypt(
                nonce,
                ciphertext,
                self._associated_data(ENVELOPE_VERSION, key_id),
            )
        except (InvalidTag, ValueError, TypeError):
            # Do not disclose whether the key, nonce, tag, or ciphertext was wrong.
            raise ReplayStateDecryptionError(
                "Encrypted replay state could not be authenticated"
            ) from None

    @staticmethod
    def _associated_data(version: int, key_id: str) -> bytes:
        return _AAD_PREFIX + str(version).encode("ascii") + b":" + key_id.encode("ascii")

    @staticmethod
    def _encode_bytes(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
