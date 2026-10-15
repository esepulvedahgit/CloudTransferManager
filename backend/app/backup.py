from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from app.backup_artifacts import decrypt_file, encrypt_file, extract_backup_tar

logger = logging.getLogger("ctm.backup")
_BACKED_UP_SECRETS = ("app_secret_key", "master_key", "postgres_password", "smtp_password")
_BUCKET_PATTERN = re.compile(r"(?=.{3,63}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])$")
_BACKUP_NAME_PATTERN = re.compile(r"ctm-backup-\d{8}T\d{6}Z-[0-9a-f-]{36}\.ctm$")


@dataclass(frozen=True)
class BackupSettings:
    bucket: str
    prefix: str
    region: str
    endpoint_url: str
    state_dir: Path
    secret_dir: Path
    env_file: Path
    database_host: str
    database_port: int
    database_name: str
    database_user: str
    database_password_file: Path
    encryption_key_file: Path
    command_path: str = "/usr/bin:/bin"
    interval_seconds: int = 600
    max_age_seconds: int = 900
    work_dir: Path | None = None

    @classmethod
    def from_environment(cls) -> BackupSettings:
        return cls(
            bucket=os.environ.get("BACKUP_S3_BUCKET", "").strip(),
            prefix=os.environ.get("BACKUP_S3_PREFIX", "").strip(),
            region=os.environ.get("BACKUP_S3_REGION", "").strip(),
            endpoint_url=os.environ.get("BACKUP_S3_ENDPOINT", "").strip().rstrip("/"),
            state_dir=Path(os.environ.get("BACKUP_STATE_DIR", "/var/lib/ctm-backups")),
            secret_dir=Path(os.environ.get("BACKUP_SECRET_DIR", "/run/ctm-secrets")),
            env_file=Path(os.environ.get("BACKUP_ENV_FILE", "/run/ctm-config/app.env")),
            database_host=os.environ.get("BACKUP_DB_HOST", "postgres").strip(),
            database_port=int(os.environ.get("BACKUP_DB_PORT", "5432")),
            database_name=os.environ.get("BACKUP_DB_NAME", "cloud_transfer").strip(),
            database_user=os.environ.get("BACKUP_DB_USER", "cloud_transfer").strip(),
            database_password_file=Path(
                os.environ.get("BACKUP_DB_PASSWORD_FILE", "/run/ctm-secrets/postgres_password")
            ),
            encryption_key_file=Path(
                os.environ.get(
                    "BACKUP_ENCRYPTION_KEY_FILE", "/run/ctm-secrets/backup_encryption_key"
                )
            ),
            command_path=os.environ.get("PATH", "/usr/bin:/bin"),
            interval_seconds=int(os.environ.get("BACKUP_INTERVAL_SECONDS", "600")),
            max_age_seconds=int(os.environ.get("BACKUP_MAX_AGE_SECONDS", "900")),
            work_dir=Path(os.environ.get("BACKUP_WORK_DIR", "/var/lib/ctm-backups/work")),
        )

    @property
    def normalized_prefix(self) -> str:
        return self.prefix.strip("/") + "/"

    def validate(self) -> None:
        from urllib.parse import urlparse

        endpoint = urlparse(self.endpoint_url)
        if endpoint.scheme != "https" or not endpoint.hostname or endpoint.path not in {"", "/"}:
            raise ValueError("BACKUP_S3_ENDPOINT must be an HTTPS S3 endpoint without a path")
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("BACKUP_S3_ENDPOINT must not contain credentials or query data")
        if not _BUCKET_PATTERN.fullmatch(self.bucket):
            raise ValueError("BACKUP_S3_BUCKET must be a valid MEGA S4 bucket name")
        if not self.prefix or self.prefix.startswith("/") or "\\" in self.prefix:
            raise ValueError("BACKUP_S3_PREFIX must be a non-empty relative prefix")
        if any(part in {"", ".", ".."} for part in self.prefix.split("/")):
            raise ValueError("BACKUP_S3_PREFIX contains an unsafe path component")
        if not all(
            (self.region, self.database_host, self.database_name, self.database_user)
        ):
            raise ValueError("MEGA S4 and PostgreSQL backup settings are incomplete")
        if self.database_port < 1 or self.database_port > 65535:
            raise ValueError("BACKUP_DB_PORT is outside the valid range")
        if self.interval_seconds < 60 or self.max_age_seconds < self.interval_seconds:
            raise ValueError(
                "Backup age must be at least the interval, and interval at least 60 seconds"
            )
        required_files = [
            self.env_file,
            self.database_password_file,
            self.encryption_key_file,
            self.secret_dir / "mega_s4_access_key",
            self.secret_dir / "mega_s4_secret_key",
            *(self.secret_dir / name for name in _BACKED_UP_SECRETS),
        ]
        missing = [str(path) for path in required_files if not path.is_file()]
        if missing:
            raise ValueError("Backup input files are missing")
        _read_backup_key(self.encryption_key_file)
        if len((self.secret_dir / "master_key").read_bytes()) != 32:
            raise ValueError("master encryption key must contain exactly 32 bytes")
        if not self.database_password_file.read_bytes().strip():
            raise ValueError("PostgreSQL password secret is empty")


@dataclass(frozen=True)
class BackupResult:
    backup_id: str
    object_key: str
    sha256: str
    size_bytes: int


def _read_backup_key(path: Path) -> bytes:
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("Backup encryption key must contain exactly 32 bytes")
    return key


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _operation_lock(settings: BackupSettings) -> Iterator[Path]:
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    work_dir = settings.work_dir or settings.state_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(settings.state_dir, 0o700)
    os.chmod(work_dir, 0o700)
    lock_path = settings.state_dir / ".backup.lock"
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(lock_descriptor)
        raise RuntimeError("another backup or restore operation is already running") from exc
    try:
        for child in work_dir.iterdir():
            if (
                child.name.startswith(("ctm-backup-", "ctm-restore-"))
                and child.is_dir()
                and not child.is_symlink()
            ):
                shutil.rmtree(child)
        yield work_dir
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def _run_dump(
    settings: BackupSettings,
    bundle_dir: Path,
    command_runner: Callable[..., Any],
) -> None:
    password = settings.database_password_file.read_text(encoding="utf-8").strip()
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    env["PATH"] = settings.command_path
    common_args = [
        "--host",
        settings.database_host,
        "--port",
        str(settings.database_port),
        "--username",
        settings.database_user,
        "--no-password",
    ]
    command_runner(
        [
            "pg_dump",
            *common_args,
            "--format=custom",
            "--no-owner",
            "--file",
            str(bundle_dir / "database.dump"),
            settings.database_name,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=1800,
        env=env,
    )
    command_runner(
        [
            "pg_dumpall",
            *common_args,
            "--globals-only",
            "--file",
            str(bundle_dir / "globals.sql"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=1800,
        env=env,
    )
    for name in ("database.dump", "globals.sql"):
        path = bundle_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("PostgreSQL backup command produced an empty artifact")
        os.chmod(path, 0o600)


def _create_bundle(
    settings: BackupSettings,
    bundle_dir: Path,
    backup_id: str,
    command_runner: Callable[..., Any],
    created_at: datetime,
) -> Path:
    _run_dump(settings, bundle_dir, command_runner)
    files: dict[str, Path] = {
        "database.dump": bundle_dir / "database.dump",
        "globals.sql": bundle_dir / "globals.sql",
        "config/app.env": settings.env_file,
    }
    files.update(
        {f"secrets/{name}": settings.secret_dir / name for name in _BACKED_UP_SECRETS}
    )
    file_hashes = {name: _sha256_file(path) for name, path in files.items()}
    manifest = {
        "format": 1,
        "backup_id": backup_id,
        "created_at_utc": created_at.isoformat(),
        "database": settings.database_name,
        "files": file_hashes,
    }
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    os.chmod(manifest_path, 0o600)
    archive_path = bundle_dir / "bundle.tar"
    with tarfile.open(archive_path, mode="w:") as archive:
        for name, path in files.items():
            archive.add(path, arcname=name, recursive=False)
        archive.add(manifest_path, arcname="manifest.json", recursive=False)
    os.chmod(archive_path, 0o600)
    return archive_path


def backup_once(
    settings: BackupSettings,
    client: Any,
    *,
    command_runner: Callable[..., Any] = subprocess.run,
    now: datetime | None = None,
) -> BackupResult:
    settings.validate()
    current_time = now or datetime.now(UTC)
    current_time = current_time.astimezone(UTC)
    backup_id = str(uuid.uuid4())
    timestamp = current_time.strftime("%Y%m%dT%H%M%SZ")
    object_key = (
        f"{settings.normalized_prefix}{current_time:%Y/%m/%d}/"
        f"ctm-backup-{timestamp}-{backup_id}.ctm"
    )
    with _operation_lock(settings) as work_dir:
        with tempfile.TemporaryDirectory(prefix="ctm-backup-", dir=work_dir) as temporary:
            bundle_dir = Path(temporary)
            archive_path = _create_bundle(
                settings, bundle_dir, backup_id, command_runner, current_time
            )
            encrypted_path = bundle_dir / "bundle.ctm"
            encrypt_file(
                archive_path, encrypted_path, _read_backup_key(settings.encryption_key_file)
            )
            ciphertext_sha256 = _sha256_file(encrypted_path)
            ciphertext_size = encrypted_path.stat().st_size
            client.upload_file(
                str(encrypted_path),
                settings.bucket,
                object_key,
                ExtraArgs={
                    "ContentType": "application/octet-stream",
                    "Metadata": {"backup-id": backup_id, "sha256": ciphertext_sha256},
                },
            )
            remote = client.head_object(Bucket=settings.bucket, Key=object_key)
            metadata = remote.get("Metadata", {})
            if (
                remote.get("ContentLength") != ciphertext_size
                or metadata.get("backup-id") != backup_id
                or metadata.get("sha256") != ciphertext_sha256
            ):
                raise RuntimeError("remote backup verification failed")

    result = BackupResult(backup_id, object_key, ciphertext_sha256, ciphertext_size)
    completed_at = datetime.now(UTC)
    _write_state(
        settings,
        {
            "last_success_at": completed_at.isoformat(),
            "backup_id": result.backup_id,
            "object_key": result.object_key,
            "size_bytes": result.size_bytes,
            "sha256": result.sha256,
            "last_error": None,
        },
    )
    return result


def _validate_manifest(output_dir: Path) -> dict[str, Any]:
    manifest_path = output_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Backup manifest is missing or malformed") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        raise ValueError("Backup format version is unsupported")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("Backup manifest has no file checksums")
    expected_files = {
        "database.dump",
        "globals.sql",
        "config/app.env",
        *(f"secrets/{name}" for name in _BACKED_UP_SECRETS),
    }
    if set(files) != expected_files:
        raise ValueError("Backup manifest has an unexpected file inventory")
    actual_files = {
        path.relative_to(output_dir).as_posix()
        for path in output_dir.rglob("*")
        if path.is_file()
    }
    if actual_files != expected_files | {"manifest.json"}:
        raise ValueError("Restored backup has an unexpected file inventory")
    for name in expected_files:
        path = output_dir / name
        if _sha256_file(path) != files[name]:
            raise ValueError("Restored backup file checksum does not match its manifest")
    protected_paths = (
        "database.dump",
        "globals.sql",
        *(f"secrets/{name}" for name in _BACKED_UP_SECRETS),
    )
    for name in protected_paths:
        os.chmod(output_dir / name, 0o600)
    return manifest


def _safe_object_key(settings: BackupSettings, object_key: str) -> None:
    if not object_key.startswith(settings.normalized_prefix):
        raise ValueError("Backup object is outside the configured prefix")
    relative = PurePosixPath(object_key[len(settings.normalized_prefix) :])
    if (
        relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or not _BACKUP_NAME_PATTERN.fullmatch(relative.name)
        or len(relative.parts) != 4
    ):
        raise ValueError("Backup object key has an invalid format")


def restore_backup(
    settings: BackupSettings,
    client: Any,
    object_key: str,
    destination: Path,
    *,
    command_runner: Callable[..., Any] = subprocess.run,
) -> BackupResult:
    settings.validate()
    _safe_object_key(settings, object_key)
    destination = destination.absolute()
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _operation_lock(settings) as work_dir, tempfile.TemporaryDirectory(
        prefix="ctm-restore-", dir=work_dir
    ) as temporary:
        temporary_dir = Path(temporary)
        encrypted_path = temporary_dir / "bundle.ctm"
        archive_path = temporary_dir / "bundle.tar"
        client.download_file(settings.bucket, object_key, str(encrypted_path))
        ciphertext_sha256 = _sha256_file(encrypted_path)
        ciphertext_size = encrypted_path.stat().st_size
        decrypt_file(encrypted_path, archive_path, _read_backup_key(settings.encryption_key_file))
        destination.mkdir(mode=0o700)
        try:
            extract_backup_tar(archive_path, destination)
            manifest = _validate_manifest(destination)
            command_runner(
                ["pg_restore", "--list", str(destination / "database.dump")],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "PATH": settings.command_path},
            )
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    return BackupResult(
        backup_id=str(manifest["backup_id"]),
        object_key=object_key,
        sha256=ciphertext_sha256,
        size_bytes=ciphertext_size,
    )


def _write_state(settings: BackupSettings, state: dict[str, Any]) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path = settings.state_dir / "latest.json"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".latest.", suffix=".partial", dir=settings.state_dir
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(state, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, state_path)
        os.chmod(state_path, 0o600)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _record_failure(settings: BackupSettings, exc: BaseException) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    current: dict[str, Any] = {}
    try:
        current = json.loads((settings.state_dir / "latest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    current["last_error_at"] = datetime.now(UTC).isoformat()
    current["last_error"] = type(exc).__name__
    _write_state(settings, current)


def is_backup_healthy(settings: BackupSettings, now: datetime | None = None) -> bool:
    try:
        state = json.loads((settings.state_dir / "latest.json").read_text(encoding="utf-8"))
        last_success = datetime.fromisoformat(state["last_success_at"])
        current = now or datetime.now(UTC)
        age = current.astimezone(UTC) - last_success.astimezone(UTC)
        return age <= timedelta(seconds=settings.max_age_seconds)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _create_s3_client(settings: BackupSettings) -> Any:
    import boto3
    from botocore.config import Config

    access_key_path = settings.secret_dir / "mega_s4_access_key"
    secret_key_path = settings.secret_dir / "mega_s4_secret_key"
    access_key = access_key_path.read_text(encoding="utf-8").strip()
    secret_key = secret_key_path.read_text(encoding="utf-8").strip()
    if not access_key or not secret_key:
        raise ValueError("MEGA S4 access-key secret files are empty")
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        region_name=settings.region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def _run_daemon(settings: BackupSettings) -> int:
    if os.environ.get("BACKUP_ENABLED", "false").strip().lower() != "true":
        logger.error("Set BACKUP_ENABLED=true after configuring MEGA S4 backup secrets")
        return 2
    with _operation_lock(settings):
        pass
    settings.validate()
    client = _create_s3_client(settings)
    while True:
        started = time.monotonic()
        try:
            result = backup_once(settings, client)
            logger.info(
                "verified encrypted database backup id=%s size_bytes=%d object=%s",
                result.backup_id,
                result.size_bytes,
                result.object_key,
            )
        except Exception as exc:
            _record_failure(settings, exc)
            logger.error("backup attempt failed (%s)", type(exc).__name__)
        time.sleep(max(1, settings.interval_seconds - (time.monotonic() - started)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ctm-s4-backup")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("daemon", help="respalda PostgreSQL a MEGA S4 periódicamente")
    commands.add_parser("once", help="ejecuta un respaldo inmediato")
    commands.add_parser("health", help="comprueba la antigüedad del último respaldo")
    restore = commands.add_parser("restore", help="descifra un respaldo en un directorio nuevo")
    restore.add_argument("--object-key", required=True)
    restore.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    settings = BackupSettings.from_environment()
    if args.command == "health":
        return 0 if is_backup_healthy(settings) else 1
    if os.environ.get("BACKUP_ENABLED", "false").strip().lower() != "true":
        logger.error("Set BACKUP_ENABLED=true after configuring MEGA S4 backup secrets")
        return 2
    try:
        if args.command == "daemon":
            return _run_daemon(settings)
        settings.validate()
        client = _create_s3_client(settings)
        if args.command == "once":
            result = backup_once(settings, client)
            logger.info("backup verified id=%s object=%s", result.backup_id, result.object_key)
            return 0
        result = restore_backup(settings, client, args.object_key, args.output)
        logger.info("backup decrypted and verified id=%s", result.backup_id)
        return 0
    except Exception as exc:
        logger.error("backup operation failed (%s)", type(exc).__name__)
        return 1


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("BACKUP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    raise SystemExit(main())
