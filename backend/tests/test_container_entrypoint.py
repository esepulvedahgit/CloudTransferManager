import os
import subprocess
from pathlib import Path


def test_entrypoint_copies_docker_secrets_to_private_runtime_files(tmp_path):
    backend_dir = Path(__file__).resolve().parents[1]
    entrypoint = backend_dir / "docker-entrypoint.sh"
    source_dir = tmp_path / "source-secrets"
    runtime_dir = tmp_path / "runtime-secrets"
    binary_dir = tmp_path / "bin"
    source_dir.mkdir()
    binary_dir.mkdir()
    (source_dir / "master_key").write_text("private key material", encoding="utf-8")
    (source_dir / "master_key").chmod(0o600)
    gosu_stub = binary_dir / "gosu"
    gosu_stub.write_text('#!/bin/sh\nshift\nexec "$@"\n', encoding="utf-8")
    gosu_stub.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{binary_dir}:{os.environ['PATH']}",
        "CTM_SOURCE_SECRETS_DIR": str(source_dir),
        "CTM_RUNTIME_SECRETS_DIR": str(runtime_dir),
        "CTM_RUNTIME_UID": str(os.getuid()),
        "CTM_RUNTIME_GID": str(os.getgid()),
        "TRANSFER_TEMP_PATH": str(tmp_path / "transfer-tmp"),
        "TRANSFER_STAGING_PATH": str(tmp_path / "transfer-staging"),
    }

    subprocess.run(["/bin/sh", str(entrypoint), "/bin/true"], env=env, check=True)

    runtime_secret = runtime_dir / "master_key"
    assert runtime_secret.read_text(encoding="utf-8") == "private key material"
    assert runtime_secret.stat().st_mode & 0o777 == 0o600
    assert runtime_secret.stat().st_uid == os.getuid()
    assert runtime_secret.stat().st_gid == os.getgid()
    assert runtime_dir.stat().st_mode & 0o777 == 0o700
    staging_dir = tmp_path / "transfer-staging"
    assert staging_dir.is_dir()
    assert staging_dir.stat().st_mode & 0o777 == 0o700
    assert staging_dir.stat().st_uid == os.getuid()
    assert staging_dir.stat().st_gid == os.getgid()
