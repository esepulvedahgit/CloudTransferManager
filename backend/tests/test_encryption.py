import uuid

import pytest
from cryptography.exceptions import InvalidTag

from app.security.credentials import CredentialCipher


def test_credential_cipher_round_trips_payload_with_connection_aad():
    cipher = CredentialCipher(b"k" * 32)
    connection_id = uuid.UUID("eb986474-e550-4685-9d99-d3742356d3f1")
    credentials = {"username": "user@example.test", "password": "provider-secret"}

    encrypted = cipher.encrypt(connection_id, credentials)

    assert encrypted.startswith(b"CTM\x02")
    assert len(encrypted) >= 4 + 12 + 48 + 12 + 16
    assert cipher.decrypt(connection_id, encrypted) == credentials
    assert b"provider-secret" not in encrypted


def test_credential_cipher_rejects_tampered_ciphertext():
    cipher = CredentialCipher(b"k" * 32)
    connection_id = uuid.uuid4()
    encrypted = bytearray(cipher.encrypt(connection_id, {"token": "secret"}))
    encrypted[-1] ^= 1

    with pytest.raises(InvalidTag):
        cipher.decrypt(connection_id, bytes(encrypted))


def test_credential_cipher_rejects_ciphertext_for_another_connection():
    cipher = CredentialCipher(b"k" * 32)
    encrypted = cipher.encrypt(uuid.uuid4(), {"refresh_token": "secret"})

    with pytest.raises(InvalidTag):
        cipher.decrypt(uuid.uuid4(), encrypted)
