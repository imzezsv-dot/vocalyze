"""AES-256-GCM with a per-job data key.

A fresh 256-bit data key is generated for every job; that key encrypts the
job's blobs, and the key itself is wrapped with the service key from
`ENCRYPTION_KEY` before it is stored beside the job. Destroying one wrapped
key makes exactly one meeting unreadable — including in any backup that
already copied the ciphertext — which is what makes erasure a cryptographic
fact rather than a promise about file deletion.

Run `python -m app.core.crypto` to mint a fresh service key.
"""

from __future__ import annotations

import base64
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SealerError(RuntimeError):
    pass


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def generate_service_key() -> str:
    return _b64(secrets.token_bytes(32))


def new_data_key() -> bytes:
    return secrets.token_bytes(32)


class Sealer:
    """Wrap the per-job data key with the service key, and encrypt blobs."""

    def __init__(self, service_key: bytes | None, enabled: bool = True):
        if enabled and (service_key is None or len(service_key) != 32):
            raise SealerError(
                "ENCRYPTION_KEY must be a 32-byte base64url value. "
                "Run `python -m app.core.crypto` to mint one."
            )
        self._key = service_key
        self.enabled = enabled

    @classmethod
    def from_settings(cls, encoded: str | None, enabled: bool) -> "Sealer":
        if not enabled:
            return cls(None, enabled=False)
        if not encoded:
            raise SealerError(
                "Encryption at rest is enabled but ENCRYPTION_KEY is not set. "
                "Run `python -m app.core.crypto` and paste the value into .env."
            )
        try:
            raw = _unb64(encoded)
        except Exception as exc:  # noqa: BLE001
            raise SealerError("ENCRYPTION_KEY is not valid base64url.") from exc
        return cls(raw, enabled=True)

    # -- data-key wrapping ------------------------------------------------
    def wrap(self, data_key: bytes, associated: bytes) -> str:
        if not self.enabled:
            return _b64(data_key)
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, data_key, associated)
        return _b64(nonce + ciphertext)

    def unwrap(self, wrapped: str, associated: bytes) -> bytes:
        raw = _unb64(wrapped)
        if not self.enabled:
            return raw
        nonce, ciphertext = raw[:12], raw[12:]
        return AESGCM(self._key).decrypt(nonce, ciphertext, associated)

    # -- blob encryption --------------------------------------------------
    def seal(self, plaintext: bytes, data_key: bytes, associated: bytes) -> bytes:
        if not self.enabled:
            return plaintext
        nonce = secrets.token_bytes(12)
        return nonce + AESGCM(data_key).encrypt(nonce, plaintext, associated)

    def open(self, ciphertext: bytes, data_key: bytes, associated: bytes) -> bytes:
        if not self.enabled:
            return ciphertext
        nonce, body = ciphertext[:12], ciphertext[12:]
        return AESGCM(data_key).decrypt(nonce, body, associated)


def _cli() -> None:
    key = os.getenv("VOCALYZE_KEY") or generate_service_key()
    print(key)


if __name__ == "__main__":
    _cli()
