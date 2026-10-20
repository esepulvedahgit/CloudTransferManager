from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.backup_verification import verify_connection_credentials
from app.security.credentials import CredentialCipher


def test_backup_verifier_decrypts_each_saved_connection_credential():
    cipher = CredentialCipher(b"m" * 32)
    connections = []
    for value in ({"refresh_token": "test-token"}, {"password": "test-password"}):
        connection_id = uuid.uuid4()
        connections.append(
            SimpleNamespace(
                id=connection_id,
                encrypted_credentials=cipher.encrypt(connection_id, value),
            )
        )

    assert verify_connection_credentials(connections, b"m" * 32) == 2


def test_backup_verifier_fails_if_any_saved_credential_cannot_be_decrypted():
    connection = SimpleNamespace(id=uuid.uuid4(), encrypted_credentials=b"corrupt")

    with pytest.raises(ValueError, match="credential decryption failed"):
        verify_connection_credentials([connection], b"m" * 32)
