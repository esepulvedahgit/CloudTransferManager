from pathlib import Path
from typing import Any

from app.providers.megacmd import MegaCmdProvider
from app.providers.mock import MockStorageProvider
from app.providers.nextcloud import NextcloudRcloneProvider
from app.providers.rclone import RcloneGoogleDriveProvider
from app.providers.seafile import SeafileRcloneProvider
from app.providers.seafile_api import SeafileApiProvider
from app.providers.smb import RcloneSmbProvider
from app.security.credentials import CredentialCipher
from app.services.megacmd import MegaCmdRunner
from app.services.memory_store import ConnectionRecord
from app.services.rclone import RcloneRunner as ServiceRcloneRunner
from app.settings import Settings


class StorageProviderFactory:
    """Build a storage adapter from one persisted connection and runtime settings."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create(self, connection: ConnectionRecord, *, runner: Any | None = None) -> Any:
        if connection.provider_type == "MOCK":
            return MockStorageProvider()
        if not connection.encrypted_credentials:
            raise ValueError("Missing provider credentials")
        credentials = CredentialCipher.from_secret_file(
            self.settings.master_encryption_key_file
        ).decrypt(connection.id, connection.encrypted_credentials)
        if connection.provider_type == "MEGA":
            return MegaCmdProvider(
                connection.id,
                credentials,
                MegaCmdRunner(self.settings.megacmd_binary_dir),
                Path(self.settings.transfer_staging_path),
            )

        current_runner = runner or ServiceRcloneRunner(
            self.settings.rclone_binary,
            self.settings.transfer_temp_path,
            self.settings.transfer_staging_path,
        )
        if connection.provider_type == "GOOGLE_DRIVE":
            return RcloneGoogleDriveProvider(connection.id, credentials, current_runner)
        if connection.provider_type == "NEXTCLOUD":
            return NextcloudRcloneProvider(
                connection.id,
                credentials,
                current_runner,
                credential_version=connection.credential_version,
                chunk_size=self.settings.nextcloud_chunk_size,
            )
        if connection.provider_type == "SMB":
            return RcloneSmbProvider(
                connection.id,
                credentials,
                current_runner,
                allowed_targets={
                    target.strip()
                    for target in self.settings.smb_allowed_targets.split(",")
                    if target.strip()
                },
                interactive_timeout_seconds=self.settings.smb_interactive_timeout_seconds,
            )
        if connection.provider_type == "SEAFILE":
            if connection.adapter_type == "SEAFILE_API":
                return SeafileApiProvider(connection.id, credentials)
            return SeafileRcloneProvider(connection.id, credentials, current_runner)
        raise ValueError("Unsupported provider")
