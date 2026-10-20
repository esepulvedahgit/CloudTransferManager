import os
import shutil
import stat
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (
        "deploy.sh",
        "setup-local-secrets.sh",
        "setup-mega-s4-backup-secrets.sh",
    ):
        shutil.copy2(PROJECT_ROOT / "scripts" / name, scripts / name)
    shutil.copy2(PROJECT_ROOT / ".env.example", tmp_path / ".env.example")
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CTM_DEPLOY_DOCKER_LOG"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    curl = binaries / "curl"
    curl.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CTM_DEPLOY_CURL_LOG"\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)

    environment = os.environ.copy()
    environment["PATH"] = f"{binaries}:{environment['PATH']}"
    environment["CTM_DEPLOY_DOCKER_LOG"] = str(tmp_path / "docker.log")
    environment["CTM_DEPLOY_CURL_LOG"] = str(tmp_path / "curl.log")
    return tmp_path, environment


def _run(root: Path, environment: dict[str, str], domain: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts" / "deploy.sh")],
        cwd=root,
        env=environment,
        input=f"{domain}\n",
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


def test_fresh_deploy_generates_domain_certificate_and_private_secrets(tmp_path: Path) -> None:
    root, environment = _sandbox(tmp_path)
    domain = "transfer.example.test"

    result = _run(root, environment, domain)

    assert result.returncode == 0, result.stdout + result.stderr
    env_file = root / ".env"
    config = env_file.read_text(encoding="utf-8")
    assert f"APP_BASE_URL=https://{domain}\n" in config
    assert "HTTP_PORT=80\n" in config
    assert "HTTPS_PORT=443\n" in config
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    secrets = root / "secrets"
    for name in ("app_secret_key", "postgres_password", "master_key", "smtp_password"):
        path = secrets / name
        assert path.stat().st_size > 0
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (secrets / "master_key").stat().st_size == 32
    assert (secrets / "backup_encryption_key").stat().st_size == 32
    assert (secrets / "mega_s4_access_key").stat().st_size == 0
    assert (secrets / "mega_s4_secret_key").stat().st_size == 0

    cert = root / "certs" / "fullchain.pem"
    key = root / "certs" / "privkey.pem"
    subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-checkhost", domain],
        check=True,
        capture_output=True,
    )
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    docker_calls = (root / "docker.log").read_text(encoding="utf-8")
    assert "compose config --quiet" in docker_calls
    assert "compose up -d --build --wait --wait-timeout 300" in docker_calls
    curl_calls = (root / "curl.log").read_text(encoding="utf-8")
    assert f"https://{domain}/health/ready" in curl_calls


def test_rerun_preserves_secrets_and_rejects_different_domain(tmp_path: Path) -> None:
    root, environment = _sandbox(tmp_path)
    domain = "transfer.example.test"
    first = _run(root, environment, domain)
    assert first.returncode == 0, first.stdout + first.stderr
    master_key = (root / "secrets" / "master_key").read_bytes()
    cert = (root / "certs" / "fullchain.pem").read_bytes()

    second = _run(root, environment, domain)
    assert second.returncode == 0, second.stdout + second.stderr
    assert (root / "secrets" / "master_key").read_bytes() == master_key
    assert (root / "certs" / "fullchain.pem").read_bytes() == cert

    rejected = _run(root, environment, "other.example.test")
    assert rejected.returncode != 0
    assert (root / "secrets" / "master_key").read_bytes() == master_key
    assert (root / "certs" / "fullchain.pem").read_bytes() == cert


def test_invalid_domain_or_partial_secret_set_does_not_start_deployment(tmp_path: Path) -> None:
    root, environment = _sandbox(tmp_path)
    invalid = _run(root, environment, "bad.example.test;touch /tmp/ctm-invalid")
    assert invalid.returncode != 0
    assert not (root / ".env").exists()
    assert "compose up" not in (root / "docker.log").read_text(encoding="utf-8")

    secrets = root / "secrets"
    secrets.mkdir()
    (secrets / "master_key").write_bytes(b"x" * 32)
    partial = _run(root, environment, "transfer.example.test")
    assert partial.returncode != 0
    assert (secrets / "master_key").read_bytes() == b"x" * 32
    assert not (root / ".env").exists()
    assert "compose up" not in (root / "docker.log").read_text(encoding="utf-8")
