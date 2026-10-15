from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol
from uuid import UUID

from app.security.credentials import CredentialCipher


class EncryptedConnection(Protocol):
    id: UUID
    encrypted_credentials: bytes


def verify_connection_credentials(
    connections: Iterable[EncryptedConnection], master_key: bytes
) -> int:
    cipher = CredentialCipher(master_key)
    verified = 0
    for connection in connections:
        if not connection.encrypted_credentials:
            continue
        try:
            cipher.decrypt(connection.id, connection.encrypted_credentials)
        except Exception as exc:
            raise ValueError(
                f"credential decryption failed for connection {connection.id}"
            ) from exc
        verified += 1
    return verified


async def _verify_database() -> int:
    from app.services.database_store import DatabaseStore
    from app.settings import get_settings

    settings = get_settings()
    store = DatabaseStore(settings)
    try:
        connections = await store.alist_connections()
        key = Path(settings.master_encryption_key_file).read_bytes()
        return verify_connection_credentials(connections, key)
    finally:
        await store.aclose()


def main() -> int:
    try:
        verified = asyncio.run(_verify_database())
    except Exception:
        print("Restored credential verification failed.", file=sys.stderr)
        return 1
    print(f"Decrypted credentials for {verified} active connection(s); values were not displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
