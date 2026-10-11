"""Check the Compose ingress and private-network invariants."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from posixpath import normpath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    raise SystemExit(message)


def network_names(service: dict[str, Any]) -> set[str]:
    networks = service.get("networks", {})
    if isinstance(networks, list):
        return set(networks)
    return set(networks)


def main() -> None:
    rendered = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    config = json.loads(rendered.stdout)
    services = config["services"]
    networks = config["networks"]

    proxy_networks = network_names(services["reverse-proxy"])
    if not {"ctm_internal", "ctm_proxy_edge"} <= proxy_networks:
        fail("reverse-proxy must join ctm_internal and ctm_proxy_edge")
    if networks["ctm_internal"].get("internal") is not True:
        fail("ctm_internal must remain internal")
    if networks["ctm_proxy_edge"].get("internal", False):
        fail("ctm_proxy_edge must be non-internal for Docker port publishing")

    proxy_ports = services["reverse-proxy"].get("ports", [])
    actual_targets = {port["target"] for port in proxy_ports}
    if actual_targets != {80, 443}:
        fail(f"reverse-proxy must publish HTTP and HTTPS targets: {actual_targets!r}")
    if any(port.get("host_ip") != "0.0.0.0" for port in proxy_ports):
        fail("reverse-proxy ports must bind to 0.0.0.0")
    if any(not port.get("published") for port in proxy_ports):
        fail("reverse-proxy host ports must be published")

    for name in ("backend", "worker", "postgres", "redis"):
        service = services[name]
        names = network_names(service)
        if "ctm_internal" not in names or "ctm_proxy_edge" in names:
            fail(f"{name} must stay on private networks, not ctm_proxy_edge")
        if service.get("ports"):
            fail(f"{name} must not publish host ports")

        if name in {"backend", "worker"}:
            staging_path = service.get("environment", {}).get("TRANSFER_STAGING_PATH")
            if not isinstance(staging_path, str) or not staging_path.startswith("/"):
                fail(f"{name} must configure an absolute TRANSFER_STAGING_PATH")
            staging_path = normpath(staging_path)
            for mount in service.get("volumes", []):
                target = normpath(mount.get("target", "/"))
                if staging_path == target or staging_path.startswith(target.rstrip("/") + "/"):
                    fail(f"{name} staging path must not use a mounted volume or bind")
            for tmpfs_mount in service.get("tmpfs", []):
                target = normpath(tmpfs_mount.split(":", 1)[0])
                if staging_path == target or staging_path.startswith(target.rstrip("/") + "/"):
                    fail(f"{name} staging path must remain disk-backed, not tmpfs")

    print("Compose network exposure is valid.")


if __name__ == "__main__":
    main()
