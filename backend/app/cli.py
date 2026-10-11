import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable, Sequence
from typing import Any

from app.security.passwords import PasswordService
from app.services.database_store import DatabaseStore
from app.settings import get_settings


async def _create_admin(store: Any, username: str, email: str, password: str) -> None:
    if await store.afind_user(username) or await store.afind_user(email):
        raise ValueError("El usuario o correo ya existe")
    await store.acreate_user(
        username=username.strip().casefold(),
        email=email.strip().casefold(),
        password_hash=PasswordService().hash(password),
        role="ADMIN",
        must_change_password=True,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    store: Any | None = None,
    password_prompt: Callable[[str], str] = getpass.getpass,
) -> int:
    parser = argparse.ArgumentParser(prog="cloud-transfer-manager")
    commands = parser.add_subparsers(dest="command", required=True)
    create_admin = commands.add_parser("create-admin", help="Crea el primer administrador")
    create_admin.add_argument("--username", required=True)
    create_admin.add_argument("--email", required=True)
    create_admin.add_argument("--password")
    args = parser.parse_args(argv)

    password = args.password or password_prompt("Contraseña inicial (mínimo 12 caracteres): ")
    if len(password) < 12 or len(password) > 128:
        print("La contraseña debe tener entre 12 y 128 caracteres.", file=sys.stderr)
        return 2

    owns_store = store is None
    current_store = store or DatabaseStore(get_settings())
    try:
        asyncio.run(_create_admin(current_store, args.username, args.email, password))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if owns_store:
            asyncio.run(current_store.aclose())
    print(f"Administrador '{args.username}' creado. Debe cambiar su contraseña al iniciar sesión.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
