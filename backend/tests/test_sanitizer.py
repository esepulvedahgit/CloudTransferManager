from app.security.sanitize import sanitize_data


def test_sanitizer_redacts_credentials_recursively_and_preserves_safe_metadata():
    result = sanitize_data(
        {
            "action": "CONNECTION_TEST",
            "password": "secret",
            "nested": {"refresh_token": "oauth-token", "client_secret": "client-secret"},
            "count": 3,
        }
    )
    assert result == {
        "action": "CONNECTION_TEST",
        "password": "[REDACTED]",
        "nested": {"refresh_token": "[REDACTED]", "client_secret": "[REDACTED]"},
        "count": 3,
    }
