import ipaddress
import re
import smtplib
import ssl
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.security.credentials import CredentialCipher
from app.services.memory_store import SMTPSettingsRecord
from app.settings import Settings


@dataclass(frozen=True)
class SMTPServerConfig:
    host: str
    port: int
    username: str
    password: str
    from_address: str
    use_tls: bool


class SMTPConnectionError(RuntimeError):
    """Sanitized SMTP connection failure safe to return to an administrator."""


def encrypt_smtp_credentials(
    settings: Settings, settings_id: uuid.UUID, username: str, password: str
) -> bytes:
    cipher = CredentialCipher.from_secret_file(settings.master_encryption_key_file)
    return cipher.encrypt(settings_id, {"username": username, "password": password})


def decrypt_smtp_credentials(settings: Settings, record: SMTPSettingsRecord) -> tuple[str, str]:
    cipher = CredentialCipher.from_secret_file(settings.master_encryption_key_file)
    credentials = cipher.decrypt(record.id, record.encrypted_credentials)
    username = credentials.get("username")
    password = credentials.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError("Encrypted SMTP credentials have an invalid format")
    return username, password


def environment_smtp_config(settings: Settings) -> SMTPServerConfig:
    password = ""
    if settings.smtp_password_file:
        try:
            password = Path(settings.smtp_password_file).read_text(encoding="utf-8").strip()
        except OSError:
            password = ""
    return SMTPServerConfig(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=password,
        from_address=settings.smtp_from_address,
        use_tls=settings.smtp_use_tls,
    )


async def load_smtp_server_config(store: Any, settings: Settings) -> SMTPServerConfig:
    record = await store.aget_smtp_settings()
    if record is None:
        return environment_smtp_config(settings)

    username = ""
    password = ""
    if record.encrypted_credentials:
        try:
            username, password = decrypt_smtp_credentials(settings, record)
        except Exception:
            raise ValueError("Saved SMTP credentials could not be loaded") from None
    return SMTPServerConfig(
        host=record.host,
        port=record.port,
        username=username,
        password=password,
        from_address=record.from_address,
        use_tls=record.use_tls,
    )


def test_smtp_connection(config: SMTPServerConfig) -> None:
    try:
        with smtplib.SMTP(config.host, config.port, timeout=10) as client:
            client.ehlo()
            if config.use_tls:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if config.username:
                client.login(config.username, config.password)
    except Exception:
        raise SMTPConnectionError("SMTP connection failed") from None


def validate_smtp_host(value: str) -> str:
    host = value.strip()
    if not host or len(host) > 255 or any(character.isspace() for character in host):
        raise ValueError("Invalid SMTP host")
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(candidate)
        return host
    except ValueError:
        pass
    if len(candidate) > 253 or not re.fullmatch(
        r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*",
        candidate,
    ):
        raise ValueError("Invalid SMTP host")
    return host


def validate_smtp_sender(value: str) -> str:
    sender = value.strip()
    if len(sender) > 255 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", sender):
        raise ValueError("Invalid sender address")
    return sender
