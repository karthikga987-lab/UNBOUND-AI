# Browser replay state encryption

Replay state can contain resolved workflow parameters and exact action arguments. Persisted state must be encrypted with AES-256-GCM; the browser backend fails closed if active key configuration is missing or invalid. There is no plaintext fallback and the application does not generate or save keys automatically.

## Development key

Generate a fresh random 32-byte development key with Python 3.13 or later:

```powershell
python -c "import base64,secrets; print('dev-v1:' + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('='))"
```

Set the complete output as `BROWSER_REPLAY_STATE_KEY` in a local `.env` file or in the process environment. The value format is `<key-id>:<base64url-encoded-32-byte-key>`. Example shape only (not a usable key):

```text
BROWSER_REPLAY_STATE_KEY=dev-v1:<base64url-encoded-32-byte-key>
```

Do not put generated keys in source control, `.env.example`, logs, or tickets. `.gitignore` excludes `.env` and `.env.*` files while retaining the non-secret `.env.example` template.

## Production configuration and rotation

Provision `BROWSER_REPLAY_STATE_KEY` through the deployment's secret manager. Do not use a development key in production. Back up keys using the organization's secret-management controls; encrypted state cannot be recovered without the key that encrypted it.

For rotation, set the new active key in `BROWSER_REPLAY_STATE_KEY`, then provide old keys for decryption only in `BROWSER_REPLAY_STATE_PREVIOUS_KEYS` as a JSON object mapping key IDs to base64url-encoded 32-byte keys:

```text
BROWSER_REPLAY_STATE_KEY=prod-v2:<new-base64url-key>
BROWSER_REPLAY_STATE_PREVIOUS_KEYS={"prod-v1":"<old-base64url-key>"}
```

New encryptions always use the active key ID. Decryption selects the key ID embedded in the authenticated envelope. Retain an old key until all state encrypted with it has been migrated or expired, then remove it from the previous-key map. Reusing a key ID for different key material is unsupported; assign a new ID for each rotation.

## Random nonce operational limit

Each encryption uses an independently generated cryptographically secure 96-bit random nonce. Random nonces have a probabilistic collision risk: for `q` encryptions under one AES-GCM key, the approximate collision probability is `q * (q - 1) / 2^97`. A nonce collision under the same key can compromise AES-GCM confidentiality and integrity.

As a conservative operational policy, rotate a key before it is used for 1,000,000 encryptions across all application instances, or after one year, whichever comes first. Count encryptions across the entire deployment, not per process. Rotate sooner after suspected key exposure. Keep the old key configured for decryption only as long as state encrypted under it must remain available.

These are documented operational limits, not enforced code limits. `BrowserReplayStateCipher` generates random nonces but does not count uses or automatically rotate keys; deployment and future replay-state storage must enforce the usage and rotation policy.
