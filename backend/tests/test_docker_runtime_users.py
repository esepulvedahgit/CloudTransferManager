from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_frontend_uses_unprivileged_nginx_on_port_8080():
    dockerfile = (PROJECT_ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")
    nginx_config = (PROJECT_ROOT / "frontend/nginx.conf").read_text(encoding="utf-8")
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
    reverse_proxy = (
        PROJECT_ROOT / "infrastructure/nginx/reverse-proxy.conf"
    ).read_text(encoding="utf-8")

    assert "FROM nginxinc/nginx-unprivileged:1.27-alpine" in dockerfile
    assert "USER 101" in dockerfile
    assert "EXPOSE 8080" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "listen 8080;" in nginx_config
    assert '      - "8080"' in compose
    assert "http://127.0.0.1:8080/" in compose
    assert "server frontend:8080;" in reverse_proxy
    assert "    healthcheck:\n      disable: true" in compose


def test_backend_root_bootstrap_has_scoped_trivy_exception_and_drops_privileges():
    dockerfile = (PROJECT_ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    lines = dockerfile.splitlines()
    root_user_line = lines.index("USER root")

    assert lines[root_user_line - 1] == "#trivy:ignore:DS-0002"
    assert any(
        "Docker Secrets" in line
        for line in lines[root_user_line - 3 : root_user_line - 1]
    )
    assert 'ENTRYPOINT ["/usr/local/bin/ctm-entrypoint"]' in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert 'exec gosu "$runtime_uid:$runtime_gid" "$@"' in (
        PROJECT_ROOT / "backend/docker-entrypoint.sh"
    ).read_text(encoding="utf-8")
