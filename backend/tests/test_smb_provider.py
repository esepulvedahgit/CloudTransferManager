import uuid

import pytest

from app.providers.base import PaginatedItems
from app.providers.smb import RcloneSmbProvider
from app.services.rclone import RcloneRunner


def credentials(**overrides: str) -> dict[str, str]:
    values = {
        "host": "nas.example.internal",
        "port": "445",
        "share": "Shared Data",
        "base_path": "Departments/Finance",
        "username": "migration-reader",
        "password": "private-password",
        "domain": "ACME",
    }
    values.update(overrides)
    return values


def provider(**overrides: str) -> RcloneSmbProvider:
    connection_id = uuid.UUID("e267fe79-086e-4519-a749-5be01e034efe")
    return RcloneSmbProvider(
        connection_id,
        credentials(**overrides),
        RcloneRunner("rclone", "/tmp/rclone"),
        allowed_targets={"nas.example.internal"},
    )


def test_smb_root_and_subfolder_are_relative_to_the_configured_share():
    smb = provider()

    assert (
        smb.remote_path("/")
        == "ctm_e267fe79086e4519a7495be01e034efe:Shared Data/Departments/Finance"
    )
    assert smb.remote_path("/2026/Reports") == (
        "ctm_e267fe79086e4519a7495be01e034efe:Shared Data/Departments/Finance/2026/Reports"
    )


def test_smb_config_obscures_password_and_contains_network_settings():
    config = provider().config_content()

    assert "type = smb" in config
    assert "host = nas.example.internal" in config
    assert "port = 445" in config
    assert "user = migration-reader" in config
    assert "domain = ACME" in config
    assert "pass = private-password" not in config
    assert "private-password" not in config


def test_smb_password_preserves_significant_surrounding_spaces():
    password = " password with spaces "
    smb = provider(password=password)

    config = smb.config_content()
    assert smb.credentials["password"] == password
    assert "pass = " in config
    assert password not in config


def test_smb_provider_rejects_path_traversal():
    smb = provider()

    with pytest.raises(ValueError, match="traversal"):
        smb.remote_path("/../../etc")


def test_smb_provider_rejects_hosts_outside_the_configured_allowlist():
    smb = RcloneSmbProvider(
        uuid.uuid4(),
        credentials(host="other.internal"),
        RcloneRunner("rclone", "/tmp/rclone"),
        allowed_targets={"nas.example.internal"},
    )

    with pytest.raises(ValueError, match="not allowed"):
        smb.config_content()


@pytest.mark.asyncio
async def test_smb_interactive_listing_uses_a_bounded_timeout():
    class CapturingRunner:
        async def run(self, *_args, **kwargs):
            assert kwargs["timeout_seconds"] == 12
            return b"[]", b""

    smb = RcloneSmbProvider(
        uuid.uuid4(),
        credentials(),
        CapturingRunner(),
        allowed_targets={"nas.example.internal"},
        interactive_timeout_seconds=12,
    )

    page = await smb.list_items("/", page_size=1)

    assert isinstance(page, PaginatedItems)
