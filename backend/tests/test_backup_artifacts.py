from __future__ import annotations

import os
import tarfile
import uuid

import pytest
from cryptography.exceptions import InvalidTag

from app.backup import BackupSettings, backup_once, restore_backup
from app.backup_artifacts import decrypt_file, encrypt_file, extract_backup_tar
from app.security.credentials import CredentialCipher


def test_backup_encryption_round_trips_large_files_and_sets_private_permissions(tmp_path):
    source = tmp_path / "database.tar"
    encrypted = tmp_path / "database.ctm"
    restored = tmp_path / "restored.tar"
    source.write_bytes(bytes(range(256)) * 20_000)
    key = b"k" * 32

    encrypt_file(source, encrypted, key)
    decrypt_file(encrypted, restored, key)

    assert restored.read_bytes() == source.read_bytes()
    assert encrypted.stat().st_mode & 0o777 == 0o600
    assert restored.stat().st_mode & 0o777 == 0o600


def test_backup_decryption_rejects_tampering_without_leaving_plaintext(tmp_path):
    source = tmp_path / "database.tar"
    encrypted = tmp_path / "database.ctm"
    restored = tmp_path / "restored.tar"
    source.write_bytes(b"database contents")
    encrypt_file(source, encrypted, b"k" * 32)
    corrupted = bytearray(encrypted.read_bytes())
    corrupted[-20] ^= 0x80
    encrypted.write_bytes(corrupted)

    with pytest.raises(InvalidTag):
        decrypt_file(encrypted, restored, b"k" * 32)

    assert not restored.exists()


def test_backup_extract_rejects_tar_path_traversal(tmp_path):
    archive = tmp_path / "malicious.tar"
    destination = tmp_path / "restore"
    destination.mkdir(mode=0o700)
    payload = tmp_path / "payload"
    payload.write_text("outside", encoding="utf-8")
    with tarfile.open(archive, "w") as tar:
        tar.add(payload, arcname="../outside")

    with pytest.raises(ValueError, match="unsafe backup archive path"):
        extract_backup_tar(archive, destination)

    assert not (tmp_path / "outside").exists()


class _MemoryS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def upload_file(self, filename, bucket, key, *, ExtraArgs):
        self.objects[(bucket, key)] = open(filename, "rb").read()
        self.metadata = ExtraArgs["Metadata"]

    def head_object(self, *, Bucket, Key):
        return {
            "ContentLength": len(self.objects[(Bucket, Key)]),
            "Metadata": self.metadata,
        }

    def download_file(self, bucket, key, filename):
        with open(filename, "wb") as output:
            output.write(self.objects[(bucket, key)])


def _write_executable(path, content):
    path.write_text(f"#!/bin/sh\n{content}\n", encoding="utf-8")
    path.chmod(0o700)


def _backup_settings(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "pg_dump",
        'while [ "$1" != "--file" ]; do shift; done; shift; printf "database" > "$1"',
    )
    _write_executable(
        bin_dir / "pg_dumpall",
        'while [ "$1" != "--file" ]; do shift; done; shift; printf "globals" > "$1"',
    )
    _write_executable(bin_dir / "pg_restore", "exit 0")
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir(mode=0o700)
    for name, value in {
        "master_key": b"m" * 32,
        "app_secret_key": b"app-session-key",
        "postgres_password": b"db-password",
        "smtp_password": b"smtp-password",
        "backup_encryption_key": b"e" * 32,
        "mega_s4_access_key": b"test-access-key",
        "mega_s4_secret_key": b"test-secret-key",
    }.items():
        (secret_dir / name).write_bytes(value)
    env_file = tmp_path / ".env"
    env_file.write_text("SMTP_USERNAME=mailer\n", encoding="utf-8")
    return BackupSettings(
        bucket="ctm-backups",
        prefix="cloud-transfer-manager",
        region="eu-amsterdam",
        endpoint_url="https://s3.eu-amsterdam.megas4.com",
        state_dir=tmp_path / "state",
        secret_dir=secret_dir,
        env_file=env_file,
        database_host="postgres",
        database_port=5432,
        database_name="cloud_transfer",
        database_user="cloud_transfer",
        database_password_file=secret_dir / "postgres_password",
        encryption_key_file=secret_dir / "backup_encryption_key",
        command_path=str(bin_dir),
    )


def test_s4_backup_uploads_only_ciphertext_and_restores_verified_bundle(tmp_path, monkeypatch):
    settings = _backup_settings(tmp_path)
    monkeypatch.setenv("PATH", f"{settings.command_path}:{os.environ['PATH']}")
    client = _MemoryS3()

    result = backup_once(settings, client)
    ciphertext = client.objects[(settings.bucket, result.object_key)]
    assert b"database" not in ciphertext
    assert b"m" * 32 not in ciphertext
    assert client.metadata["sha256"] == result.sha256

    restored_dir = tmp_path / "restored"
    restored = restore_backup(settings, client, result.object_key, restored_dir)

    assert restored.backup_id == result.backup_id
    assert restored.sha256 == result.sha256
    assert restored.size_bytes == result.size_bytes
    assert (restored_dir / "database.dump").read_bytes() == b"database"
    assert (restored_dir / "globals.sql").read_bytes() == b"globals"
    assert (restored_dir / "secrets" / "master_key").read_bytes() == b"m" * 32
    assert (restored_dir / "secrets" / "app_secret_key").read_bytes() == b"app-session-key"
    assert (restored_dir / "config" / "app.env").read_text() == "SMTP_USERNAME=mailer\n"
    connection_id = uuid.uuid4()
    cipher = CredentialCipher((restored_dir / "secrets" / "master_key").read_bytes())
    encrypted_credentials = CredentialCipher(b"m" * 32).encrypt(
        connection_id, {"refresh_token": "test-only-token"}
    )
    assert cipher.decrypt(connection_id, encrypted_credentials) == {
        "refresh_token": "test-only-token"
    }


def test_s4_backup_fails_closed_if_remote_object_does_not_match_upload_metadata(tmp_path):
    settings = _backup_settings(tmp_path)
    client = _MemoryS3()
    original_head = client.head_object

    def wrong_head(**kwargs):
        result = original_head(**kwargs)
        result["ContentLength"] += 1
        return result

    client.head_object = wrong_head
    with pytest.raises(RuntimeError, match="remote backup verification failed"):
        backup_once(settings, client)

    assert not (settings.state_dir / "latest.json").exists()


def test_s4_backup_rejects_an_invalid_master_key_before_upload(tmp_path):
    settings = _backup_settings(tmp_path)
    (settings.secret_dir / "master_key").write_bytes(b"short-key")
    client = _MemoryS3()

    with pytest.raises(ValueError, match="master encryption key must contain exactly 32 bytes"):
        backup_once(settings, client)

    assert not client.objects


def test_restore_rejects_an_object_outside_the_configured_s4_prefix(tmp_path):
    settings = _backup_settings(tmp_path)

    with pytest.raises(ValueError, match="outside the configured prefix"):
        restore_backup(
            settings,
            _MemoryS3(),
            "another-prefix/2026/10/14/ctm-backup-x.ctm",
            tmp_path / "restore",
        )
