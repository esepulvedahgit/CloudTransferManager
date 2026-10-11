from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.providers.rclone import RcloneStorageProvider, _config_value
from app.providers.seafile_auth import normalize_seafile_base_url

_RCLONE_OBSCURE_KEY = bytes.fromhex(
    "9c935b48730a554d6bfd7c63c886a92bd390198eb8128afbf4de162b8b95f638"
)


def _obscure_for_rclone(value: str) -> str:
    """Match rclone's AES-CTR config obscuring for backend password options."""
    iv = os.urandom(16)
    encryptor = Cipher(algorithms.AES(_RCLONE_OBSCURE_KEY), modes.CTR(iv)).encryptor()
    encoded = iv + encryptor.update(value.encode("utf-8")) + encryptor.finalize()
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


class SeafileRcloneProvider(RcloneStorageProvider):
    provider_type = "SEAFILE"

    def config_content(self) -> str:
        url = self.credentials.get("url")
        username = self.credentials.get("username")
        token = self.credentials.get("auth_token")
        if not all(isinstance(value, str) and value for value in (url, username, token)):
            raise ValueError("Seafile URL, username, and account token are required")
        assert isinstance(url, str)
        assert isinstance(username, str)
        assert isinstance(token, str)
        normalized_url = normalize_seafile_base_url(url)
        if not normalized_url:
            raise ValueError("Seafile server URL is required")
        lines = [
            f"[{self.remote_name}]",
            "type = seafile",
            f"url = {_config_value(normalized_url + '/', 'url')}",
            f"user = {_config_value(username, 'username')}",
            f"auth_token = {_config_value(token, 'auth_token')}",
        ]
        library = self.credentials.get("library")
        if library:
            if not isinstance(library, str):
                raise ValueError("Invalid Seafile library")
            lines.append(f"library = {_config_value(library, 'library')}")
        library_password = self.credentials.get("library_password")
        if library_password:
            if not isinstance(library_password, str):
                raise ValueError("Invalid Seafile library password")
            lines.append(f"library_key = {_obscure_for_rclone(library_password)}")
        return "\n".join(lines) + "\n"
