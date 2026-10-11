from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx


class SeafileAuthenticationError(RuntimeError):
    """A sanitized Seafile authentication failure safe to surface to callers."""

    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__("Seafile authentication failed")


def normalize_seafile_base_url(base_url: str) -> str:
    value = base_url.strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid Seafile server URL")
    if any(character in value for character in "\r\n\x00"):
        raise ValueError("Invalid Seafile server URL")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


class SeafileAuthClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.base_url = normalize_seafile_base_url(base_url)
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    async def obtain_token(self, username: str, password: str, otp: str | None = None) -> str:
        headers = {"Accept": "application/json"}
        if otp:
            headers["X-SEAFILE-OTP"] = otp
        endpoint = f"{self.base_url}/api2/auth-token/"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    endpoint,
                    data={"username": username, "password": password},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise SeafileAuthenticationError() from exc
        if response.status_code != 200:
            raise SeafileAuthenticationError(response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SeafileAuthenticationError(response.status_code) from exc
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise SeafileAuthenticationError(response.status_code)
        return token

    async def ping(self, token: str) -> bool:
        endpoint = f"{self.base_url}/api2/auth/ping/"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    endpoint,
                    headers={"Authorization": f"Token {token}"},
                )
        except httpx.HTTPError as exc:
            raise SeafileAuthenticationError() from exc
        if response.status_code in {401, 403}:
            return False
        if response.status_code != 200:
            raise SeafileAuthenticationError(response.status_code)
        return True
