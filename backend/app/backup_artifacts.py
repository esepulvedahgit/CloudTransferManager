from __future__ import annotations

import os
import secrets
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_MAGIC = b"CTM-S4-BACKUP\x01"
_NONCE_SIZE = 12
_TAG_SIZE = 16
_CHUNK_SIZE = 1024 * 1024


def _validate_paths(source: Path, destination: Path, key: bytes) -> None:
    if len(key) != 32:
        raise ValueError("Backup encryption key must contain exactly 32 bytes")
    if source.resolve() == destination.resolve():
        raise ValueError("Source and destination must be different files")
    if destination.exists():
        raise FileExistsError(destination)


def _atomic_write(destination: Path, writer: Callable[[BinaryIO], None]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            writer(output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, destination)
        os.chmod(destination, 0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def encrypt_file(source: Path, destination: Path, key: bytes) -> None:
    """Stream an AES-256-GCM encrypted backup artifact to a private file."""
    _validate_paths(source, destination, key)
    nonce = secrets.token_bytes(_NONCE_SIZE)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(_MAGIC)

    def write_encrypted(output: BinaryIO) -> None:
        output.write(_MAGIC + nonce)
        with source.open("rb") as plaintext:
            while chunk := plaintext.read(_CHUNK_SIZE):
                output.write(encryptor.update(chunk))
        output.write(encryptor.finalize())
        output.write(encryptor.tag)

    _atomic_write(destination, write_encrypted)


def decrypt_file(source: Path, destination: Path, key: bytes) -> None:
    """Authenticate and decrypt an artifact without publishing partial plaintext."""
    _validate_paths(source, destination, key)
    minimum_size = len(_MAGIC) + _NONCE_SIZE + _TAG_SIZE
    source_size = source.stat().st_size
    if source_size < minimum_size:
        raise ValueError("Encrypted backup artifact is truncated")

    with source.open("rb") as encrypted:
        header = encrypted.read(len(_MAGIC) + _NONCE_SIZE)
        if len(header) != len(_MAGIC) + _NONCE_SIZE or not header.startswith(_MAGIC):
            raise ValueError("Encrypted backup artifact has an unsupported format")
        nonce = header[len(_MAGIC) :]
        ciphertext_size = source_size - len(header) - _TAG_SIZE
        encrypted.seek(source_size - _TAG_SIZE)
        tag = encrypted.read(_TAG_SIZE)
        encrypted.seek(len(header))

        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(_MAGIC)

        def write_plaintext(output: BinaryIO) -> None:
            remaining = ciphertext_size
            while remaining:
                chunk = encrypted.read(min(_CHUNK_SIZE, remaining))
                if not chunk:
                    raise ValueError("Encrypted backup artifact is truncated")
                output.write(decryptor.update(chunk))
                remaining -= len(chunk)
            output.write(decryptor.finalize())

        _atomic_write(destination, write_plaintext)


def extract_backup_tar(archive: Path, destination: Path) -> None:
    """Extract regular files only, rejecting traversal, links, and overwrites."""
    root = destination.resolve()
    if not destination.is_dir() or any(destination.iterdir()):
        raise ValueError("Backup extraction directory must exist and be empty")

    seen: set[Path] = set()
    with tarfile.open(archive, mode="r:") as tar:
        members = tar.getmembers()
        for member in members:
            member_path = Path(member.name)
            if (
                member_path.is_absolute()
                or not member_path.parts
                or any(part in {"", ".", ".."} for part in member_path.parts)
            ):
                raise ValueError("unsafe backup archive path")
            target = (root / member_path).resolve()
            if target != root and root not in target.parents:
                raise ValueError("unsafe backup archive path")
            if target in seen:
                raise ValueError("duplicate backup archive path")
            seen.add(target)
            if not (member.isfile() or member.isdir()):
                raise ValueError("backup archive contains a non-regular file")

        for member in members:
            target = root / Path(member.name)
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                raise ValueError("backup archive contains an unreadable file")
            with extracted, target.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                shutil.copyfileobj(extracted, output, _CHUNK_SIZE)
