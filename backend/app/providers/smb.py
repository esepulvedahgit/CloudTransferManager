import uuid
from typing import Any

from app.providers.mock import MockStorageProvider
from app.providers.rclone import RcloneStorageProvider, _config_value
from app.services.rclone import RcloneRunner


class RcloneSmbProvider(RcloneStorageProvider):
    """Read-only logical-root view of one configured SMB share."""

    provider_type = "SMB"
    _required_credentials = {"host", "share", "username", "password"}
    _optional_credentials = {"port", "base_path", "domain"}

    def __init__(
        self,
        connection_id: uuid.UUID,
        credentials: dict[str, Any],
        runner: RcloneRunner,
        *,
        allowed_targets: set[str] | frozenset[str],
        interactive_timeout_seconds: float = 30,
    ) -> None:
        super().__init__(
            connection_id,
            self.validate_credentials(credentials),
            runner,
            operation_timeout_seconds=interactive_timeout_seconds,
        )
        self.allowed_targets = {target.casefold().rstrip(".") for target in allowed_targets}

    @classmethod
    def validate_credentials(cls, credentials: dict[str, Any]) -> dict[str, str]:
        unknown = set(credentials) - cls._required_credentials - cls._optional_credentials
        missing = cls._required_credentials - set(credentials)
        if unknown or missing:
            raise ValueError("Invalid SMB connection fields")
        result: dict[str, str] = {}
        for field_name in cls._required_credentials | cls._optional_credentials:
            raw = credentials.get(field_name)
            if raw is None:
                continue
            if not isinstance(raw, str) or any(c in raw for c in "\r\n\x00"):
                raise ValueError(f"Invalid SMB {field_name}")
            result[field_name] = raw if field_name == "password" else raw.strip()
        for field_name in ("host", "share", "username", "password"):
            if not result.get(field_name):
                raise ValueError(f"SMB {field_name} is required")
        host = result["host"].casefold().rstrip(".")
        if "/" in host or "\\" in host or "://" in host:
            raise ValueError("Invalid SMB host")
        result["host"] = host
        share = result["share"]
        if share in {".", ".."} or "/" in share or "\\" in share:
            raise ValueError("Invalid SMB share")
        result["port"] = result.get("port", "445") or "445"
        try:
            port = int(result["port"])
        except ValueError as exc:
            raise ValueError("Invalid SMB port") from exc
        if not 1 <= port <= 65535:
            raise ValueError("Invalid SMB port")
        result["port"] = str(port)
        base_path = result.get("base_path", "")
        if base_path:
            normalized = MockStorageProvider.normalize_path(base_path)
            result["base_path"] = normalized.lstrip("/")
        else:
            result["base_path"] = ""
        return result

    def remote_path(self, path: str) -> str:
        normalized = MockStorageProvider.normalize_path(path)
        parts = [self.credentials["share"]]
        if self.credentials["base_path"]:
            parts.append(self.credentials["base_path"])
        if normalized != "/":
            parts.append(normalized.lstrip("/"))
        return f"{self.remote_name}:{'/'.join(parts)}"

    def config_content(self) -> str:
        host = self.credentials["host"]
        if host not in self.allowed_targets:
            raise ValueError("SMB target is not allowed")
        lines = [
            f"[{self.remote_name}]",
            "type = smb",
            f"host = {_config_value(host, 'host')}",
            f"user = {_config_value(self.credentials['username'], 'username')}",
            f"pass = {_obscure_smb_password(self.credentials['password'])}",
            f"port = {self.credentials['port']}",
        ]
        domain = self.credentials.get("domain")
        if domain:
            lines.append(f"domain = {_config_value(domain, 'domain')}")
        return "\n".join(lines) + "\n"


def _obscure_smb_password(password: str) -> str:
    from app.providers.seafile import _obscure_for_rclone

    return _obscure_for_rclone(password)
