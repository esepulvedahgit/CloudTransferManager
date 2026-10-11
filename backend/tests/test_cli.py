import asyncio

from app.cli import main
from app.services.database_store import DatabaseStore
from app.settings import Settings


def test_create_admin_cli_creates_argon2id_user_without_default_password(tmp_path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'cli.sqlite'}")
    store = DatabaseStore(settings)
    asyncio.run(store.create_schema())
    result = main(
        [
            "create-admin",
            "--username",
            "initial-admin",
            "--email",
            "initial-admin@example.test",
            "--password",
            "Secret-admin-password123!",
        ],
        store=store,
    )
    assert result == 0
    user = asyncio.run(store.afind_user("initial-admin"))
    assert user is not None
    assert user.role == "ADMIN"
    assert user.must_change_password is True
    assert user.password_hash.startswith("$argon2id$")
    asyncio.run(store.aclose())
