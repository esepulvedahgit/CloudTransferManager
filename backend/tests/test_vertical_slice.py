import asyncio
import uuid

import httpx
import pytest

from app.main import create_app
from app.security.passwords import PasswordService
from app.services.memory_store import MemoryStore
from app.settings import Settings


@pytest.mark.asyncio
async def test_login_mock_navigation_copy_progress_and_history():
    store = MemoryStore()
    store.add_user(
        username="operator",
        email="operator@example.test",
        password_hash=PasswordService().hash("CorrectHorseBatteryStaple123!"),
        role="ADMIN",
    )
    app = create_app(
        Settings(app_secret_key="test-secret-key", session_cookie_secure=False), store=store
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "username_or_email": "operator",
                "password": "CorrectHorseBatteryStaple123!",
            },
            headers=headers,
        )
        assert login.status_code == 200
        assert login.json()["role"] == "ADMIN"
        assert (await client.get("/api/v1/auth/me")).json()["username"] == "operator"
        headers = {"X-CSRF-Token": client.cookies["ctm_csrf"]}

        source_response = await client.post(
            "/api/v1/connections",
            json={"name": "Origen", "provider_type": "MOCK"},
            headers=headers,
        )
        assert source_response.status_code == 201, source_response.text
        source = source_response.json()
        destination_response = await client.post(
            "/api/v1/connections",
            json={"name": "Destino", "provider_type": "MOCK"},
            headers=headers,
        )
        assert destination_response.status_code == 201, destination_response.text
        destination = destination_response.json()
        items = await client.get(f"/api/v1/connections/{source['id']}/items?path=/Proyectos")
        assert [item["name"] for item in items.json()["items"]] == [
            "informe.pdf",
            "video_demo.mp4",
        ]

        transfer = await client.post(
            "/api/v1/transfers",
            json={
                "source_connection_id": source["id"],
                "destination_connection_id": destination["id"],
                "source_paths": ["/Proyectos/informe.pdf"],
                "destination_path": "/Copias",
                "operation": "COPY",
                "send_email_notification": True,
            },
            headers=headers,
        )
        assert transfer.status_code == 201
        assert transfer.json()["public_id"].startswith("TRF-")
        assert store.get_transfer(uuid.UUID(transfer.json()["id"])).send_email_notification

        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            current = (await client.get(f"/api/v1/transfers/{transfer.json()['id']}")).json()
            if current["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.02)
        assert current["status"] == "COMPLETED"
        assert current["bytes_transferred"] == 1048576
        history = (await client.get("/api/v1/transfers")).json()["items"]
        assert history[0]["id"] == transfer.json()["id"]
        events = await client.get(f"/api/v1/transfers/{transfer.json()['id']}/events")
        assert events.status_code == 200
        assert any(event["event_type"] == "COMPLETED" for event in events.json()["items"])
