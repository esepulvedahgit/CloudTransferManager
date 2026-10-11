import httpx
import pytest

from app.providers.seafile_auth import SeafileAuthClient, SeafileAuthenticationError


@pytest.mark.asyncio
async def test_obtain_token_posts_credentials_and_transient_otp_to_seafile():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"token": "account-token"})

    client = SeafileAuthClient(
        "https://files.example.test/seafile",
        transport=httpx.MockTransport(handler),
    )

    token = await client.obtain_token("owner@example.test", "p@ss word", "123456")

    request = requests[0]
    assert token == "account-token"
    assert str(request.url) == "https://files.example.test/seafile/api2/auth-token/"
    assert request.headers["X-SEAFILE-OTP"] == "123456"
    assert b"username=owner%40example.test" in request.content
    assert b"password=p%40ss+word" in request.content


@pytest.mark.asyncio
async def test_obtain_token_omits_otp_header_when_not_needed():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"token": "account-token"})

    client = SeafileAuthClient(
        "https://files.example.test/",
        transport=httpx.MockTransport(handler),
    )

    await client.obtain_token("owner@example.test", "password")

    assert "X-SEAFILE-OTP" not in requests[0].headers


@pytest.mark.asyncio
async def test_authentication_error_does_not_include_credentials_or_response_body():
    client = SeafileAuthClient(
        "https://files.example.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"detail": "password=p@ss word OTP=123456"})
        ),
    )

    with pytest.raises(SeafileAuthenticationError) as error:
        await client.obtain_token("owner@example.test", "p@ss word", "123456")

    assert "p@ss word" not in str(error.value)
    assert "123456" not in str(error.value)
    assert "password=" not in str(error.value)


@pytest.mark.asyncio
async def test_auth_ping_uses_account_token_and_returns_false_on_unauthorized():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"detail": "unauthorized"})

    client = SeafileAuthClient("https://files.example.test", transport=httpx.MockTransport(handler))

    assert await client.ping("expired-token") is False
    assert str(requests[0].url) == "https://files.example.test/api2/auth/ping/"
    assert requests[0].headers["Authorization"] == "Token expired-token"
