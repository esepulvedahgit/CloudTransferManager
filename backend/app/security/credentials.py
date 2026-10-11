import json
import secrets
import uuid
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialCipher:
    """AES-256-GCM envelope encryption, bound to a connection UUID."""

    _prefix = b"CTM\x02"
    _legacy_prefix = b"CTM\x01"
    _nonce_size = 12

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise ValueError("The master encryption key must contain exactly 32 bytes")
        self._aes = AESGCM(master_key)

    @classmethod
    def from_secret_file(cls, path: str | Path) -> "CredentialCipher":
        return cls(Path(path).read_bytes())

    def encrypt(self, connection_id: uuid.UUID, credentials: dict[str, Any]) -> bytes:
        data_key = AESGCM.generate_key(bit_length=256)
        wrap_nonce = secrets.token_bytes(self._nonce_size)
        data_nonce = secrets.token_bytes(self._nonce_size)
        plaintext = json.dumps(credentials, separators=(",", ":"), sort_keys=True).encode()
        wrap_aad = b"CTM:DEK:" + connection_id.bytes
        wrapped_key = self._aes.encrypt(wrap_nonce, data_key, wrap_aad)
        ciphertext = AESGCM(data_key).encrypt(data_nonce, plaintext, connection_id.bytes)
        return self._prefix + wrap_nonce + wrapped_key + data_nonce + ciphertext

    def decrypt(self, connection_id: uuid.UUID, encrypted: bytes) -> dict[str, Any]:
        if encrypted.startswith(self._legacy_prefix):
            nonce_start = len(self._legacy_prefix)
            nonce_end = nonce_start + self._nonce_size
            if len(encrypted) < nonce_end + 16:
                raise ValueError("Encrypted credentials have an invalid format")
            plaintext = self._aes.decrypt(
                encrypted[nonce_start:nonce_end],
                encrypted[nonce_end:],
                connection_id.bytes,
            )
            return self._decode(plaintext)

        minimum_size = len(self._prefix) + self._nonce_size + 48 + self._nonce_size + 16
        if len(encrypted) < minimum_size or not encrypted.startswith(self._prefix):
            raise ValueError("Encrypted credentials have an invalid format")
        cursor = len(self._prefix)
        wrap_nonce = encrypted[cursor : cursor + self._nonce_size]
        cursor += self._nonce_size
        wrapped_key = encrypted[cursor : cursor + 48]
        cursor += 48
        data_nonce = encrypted[cursor : cursor + self._nonce_size]
        cursor += self._nonce_size
        data_key = self._aes.decrypt(
            wrap_nonce,
            wrapped_key,
            b"CTM:DEK:" + connection_id.bytes,
        )
        plaintext = AESGCM(data_key).decrypt(
            data_nonce,
            encrypted[cursor:],
            connection_id.bytes,
        )
        return self._decode(plaintext)

    @staticmethod
    def _decode(plaintext: bytes) -> dict[str, Any]:
        result = json.loads(plaintext)
        if not isinstance(result, dict):
            raise ValueError("Credential payload must be a JSON object")
        return result
