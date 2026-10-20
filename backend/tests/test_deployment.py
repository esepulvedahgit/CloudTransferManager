import asyncio
from pathlib import Path

import uvicorn
import yaml


def test_uvicorn_uses_asyncio_for_megacmd_stdin_compatibility():
    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "compose.yaml").read_text())
    command = compose["services"]["backend"]["command"][-1].split()
    loop_option = command[command.index("--loop") + 1] if "--loop" in command else "auto"
    factory = uvicorn.Config("app.main:app", loop=loop_option).get_loop_factory()

    async def uses_standard_subprocess_loop():
        return isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)

    with asyncio.Runner(loop_factory=factory) as runner:
        assert runner.run(uses_standard_subprocess_loop())


def test_scheduler_and_control_worker_are_internal_services_without_published_ports():
    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "compose.yaml").read_text())
    services = compose["services"]

    assert "beat" in services
    assert "beat" in " ".join(services["beat"]["command"])
    assert "control-worker" in services
    control_worker = services["control-worker"]
    assert "control" in " ".join(control_worker["command"])
    assert not control_worker.get("ports")
    assert "ctm_internal" in control_worker["networks"]
    assert not services["beat"].get("ports")
    assert "ctm_internal" in services["beat"]["networks"]
    assert services["beat"]["healthcheck"]["disable"] is True

    notification_worker = services["notification-worker"]
    assert "notifications" in " ".join(notification_worker["command"])
    assert not notification_worker.get("ports")
    assert "ctm_internal" in notification_worker["networks"]
    assert "provider_egress" in notification_worker["networks"]
    assert {"postgres_password", "master_key", "smtp_password"}.issubset(
        set(notification_worker["secrets"])
    )


def test_compose_services_have_resource_budgets_and_rotated_logs():
    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "compose.yaml").read_text())
    for service in compose["services"].values():
        limits = service["deploy"]["resources"]["limits"]
        assert limits["cpus"]
        assert limits["memory"]
        assert limits["pids"] > 0
        assert service["logging"]["driver"] == "json-file"
        assert service["logging"]["options"] == {"max-size": "20m", "max-file": "5"}

    redis_command = " ".join(compose["services"]["redis"]["command"])
    assert "--maxmemory" in redis_command
    assert "--maxmemory-policy noeviction" in redis_command


def test_backup_scratch_files_live_only_in_a_private_tmpfs():
    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "compose.yaml").read_text())
    backup = compose["services"]["backup"]

    assert backup["environment"]["BACKUP_WORK_DIR"] == "/run/ctm-backup-work"
    assert any(
        mount.startswith("/run/ctm-backup-work:")
        for mount in backup["tmpfs"]
    )
    assert all(
        "/run/ctm-backup-work" not in mount
        for mount in backup.get("volumes", [])
    )
