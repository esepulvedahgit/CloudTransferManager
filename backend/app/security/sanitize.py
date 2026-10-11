from typing import Any

_SENSITIVE_FRAGMENTS = (
    "password",
    "passwd",
    "token",
    "secret",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "master_key",
    "private_key",
)


def sanitize_data(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, nested in value.items():
            safe_key = str(key)[:128]
            normalized = safe_key.casefold().replace("-", "_")
            clean[safe_key] = (
                "[REDACTED]"
                if any(fragment in normalized for fragment in _SENSITIVE_FRAGMENTS)
                else sanitize_data(nested, _depth=_depth + 1)
            )
        return clean
    if isinstance(value, (list, tuple)):
        return [sanitize_data(item, _depth=_depth + 1) for item in value[:1000]]
    if isinstance(value, str):
        return value[:2000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2000]
