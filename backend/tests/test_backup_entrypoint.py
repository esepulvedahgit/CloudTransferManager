from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.getuid() != 0, reason="The backup entrypoint stages host secrets as root")
def test_backup_entrypoint_stages_secrets_and_private_state_before_dropping_privileges(tmp_path):
    entrypoint = Path(__file__).resolve().parents[2] / "ops" / "backup" / "entrypoint.sh"
    source_dir = tmp_path / "source-secrets"
    runtime_dir = tmp_path / "runtime-secrets"
    config_source = tmp_path / "source.env"
    binary_dir = tmp_path / "bin"
    source_dir.mkdir()
    binary_dir.mkdir()
    secret_values = {
        "app_secret_key": b"session-key",
        "postgres_password": b"database-password",
        "master_key": b"m" * 32,
        "smtp_password": b"smtp-password",
        "backup_encryption_key": b"e" * 32,
        "mega_s4_access_key": b"s4-access-key",
        "mega_s4_secret_key": b"s4-secret-key",
    }
    for name, value in secret_values.items():
        (source_dir / name).write_bytes(value)
        (source_dir / name).chmod(0o600)
    config_source.write_text("BACKUP_S3_BUCKET=ctm-backups\n", encoding="utf-8")
    (source_dir / "unused-secret").write_text("must not be copied", encoding="utf-8")
    gosu_stub = binary_dir / "gosu"
    gosu_stub.write_text(
        "#!/bin/sh\nprintf '%s' \"$1\" > \"$GOSU_ARGUMENT_FILE\"\nshift\nexec \"$@\"\n",
        encoding="utf-8",
    )
    gosu_stub.chmod(0o700)
    state_dir = tmp_path / "backup-state"
    work_dir = tmp_path / "backup-work"
    env = {
        **os.environ,
        "PATH": f"{binary_dir}:{os.environ['PATH']}",
        "CTM_BACKUP_SOURCE_SECRETS_DIR": str(source_dir),
        "CTM_BACKUP_SOURCE_ENV_FILE": str(config_source),
        "CTM_BACKUP_RUNTIME_DIR": str(runtime_dir),
        "CTM_BACKUP_UID": str(os.getuid()),
        "CTM_BACKUP_GID": str(os.getgid()),
        "GOSU_ARGUMENT_FILE": str(tmp_path / "gosu-arguments"),
        "BACKUP_STATE_DIR": str(state_dir),
        "BACKUP_WORK_DIR": str(work_dir),
    }

    subprocess.run(["/bin/sh", str(entrypoint), "/bin/true"], env=env, check=True)

    for name, value in secret_values.items():
        staged = runtime_dir / name
        assert staged.read_bytes() == value
        assert staged.stat().st_mode & 0o777 == 0o600
        assert staged.stat().st_uid == os.getuid()
    assert (runtime_dir / "app.env").read_text(encoding="utf-8") == "BACKUP_S3_BUCKET=ctm-backups\n"
    assert not (runtime_dir / "unused-secret").exists()
    assert (tmp_path / "gosu-arguments").read_text(encoding="utf-8") == (
        f"{os.getuid()}:{os.getgid()}"
    )
    for private_dir in (runtime_dir, state_dir, work_dir):
        assert private_dir.stat().st_mode & 0o777 == 0o700
