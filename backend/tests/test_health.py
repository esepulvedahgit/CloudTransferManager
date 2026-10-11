import httpx
import pytest

from app.main import create_app
from app.services.memory_store import MemoryStore
from app.settings import Settings


def _fake_mega_binaries(tmp_path):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    for binary in (
        "mega-cmd",
        "mega-ls",
        "mega-get",
        "mega-put",
        "mega-df",
        "mega-logout",
        "mega-quit",
    ):
        executable = binary_dir / binary
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    return binary_dir


@pytest.mark.asyncio
async def test_production_readiness_requires_a_live_celery_worker(monkeypatch, tmp_path):
    settings = Settings(
        app_env="production",
        app_secret_key="test-secret",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.sqlite'}",
        redis_url="redis://localhost:6379/0",
        rclone_binary="/bin/true",
        megacmd_binary_dir=str(_fake_mega_binaries(tmp_path)),
        session_cookie_secure=False,
    )
    app = create_app(settings, store=MemoryStore())

    class ReadyBus:
        async def ping(self):
            return True

    app.state.event_bus = ReadyBus()
    monkeypatch.setattr("app.worker.celery_app.control.ping", lambda timeout: [])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    await app.state.redis_client.aclose()


@pytest.mark.asyncio
async def test_production_readiness_accepts_a_live_celery_worker(monkeypatch, tmp_path):
    binary_dir = _fake_mega_binaries(tmp_path)
    settings = Settings(
        app_env="production",
        app_secret_key="test-secret",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.sqlite'}",
        redis_url="redis://localhost:6379/0",
        rclone_binary="/bin/true",
        megacmd_binary_dir=str(binary_dir),
        session_cookie_secure=False,
    )
    app = create_app(settings, store=MemoryStore())

    class ReadyBus:
        async def ping(self):
            return True

    app.state.event_bus = ReadyBus()
    monkeypatch.setattr(
        "app.worker.celery_app.control.ping", lambda timeout: [{"worker@localhost": {"ok": "pong"}}]
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    await app.state.redis_client.aclose()


@pytest.mark.asyncio
async def test_production_readiness_requires_mega_cmd_binary(monkeypatch, tmp_path):
    settings = Settings(
        app_env="production",
        app_secret_key="test-secret",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.sqlite'}",
        redis_url="redis://localhost:6379/0",
        rclone_binary="/bin/true",
        megacmd_binary_dir=str(tmp_path / "missing-bin"),
        session_cookie_secure=False,
    )
    app = create_app(settings, store=MemoryStore())

    class ReadyBus:
        async def ping(self):
            return True

    app.state.event_bus = ReadyBus()
    monkeypatch.setattr(
        "app.worker.celery_app.control.ping", lambda timeout: [{"worker@localhost": {"ok": "pong"}}]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    await app.state.redis_client.aclose()
