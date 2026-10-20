ARG GO_VERSION=1.26.9
# Commit for the upstream Gosu 1.19 release (its tag is not Go-module-prefixed).
ARG GOSU_REVISION=6456aaa0f3c854d199d0f037f068eb97515b7513

FROM --platform=$BUILDPLATFORM golang:${GO_VERSION}-bookworm AS gosu-builder
ARG GO_VERSION
ARG TARGETOS
ARG TARGETARCH
ARG GOSU_REVISION

RUN set -eux; \
    test "$(go env GOVERSION)" = "go${GO_VERSION}"; \
    CGO_ENABLED=0 GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" \
        go install -trimpath -ldflags="-s -w" "github.com/tianon/gosu@${GOSU_REVISION}"; \
    /go/bin/gosu --version

FROM postgres:16-bookworm

COPY --from=gosu-builder /go/bin/gosu /usr/local/bin/gosu

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates python3 python3-boto3 python3-cryptography \
    && rm -rf /var/lib/apt/lists/*

RUN gosu --version && gosu nobody true

WORKDIR /opt/ctm
COPY backend/app/__init__.py ./app/__init__.py
COPY backend/app/backup.py ./app/backup.py
COPY backend/app/backup_artifacts.py ./app/backup_artifacts.py
COPY ops/backup/entrypoint.sh /usr/local/bin/ctm-backup-entrypoint

RUN mkdir -p /var/lib/ctm-backups \
    && chmod 0700 /var/lib/ctm-backups \
    && chmod 0755 /usr/local/bin/ctm-backup-entrypoint

ENV PYTHONPATH=/opt/ctm
# The entrypoint briefly needs root to copy file-backed Docker Secrets into tmpfs,
# then it drops to the postgres user with gosu before starting the backup process.
#trivy:ignore:DS-0002
USER root
ENTRYPOINT ["/usr/local/bin/ctm-backup-entrypoint"]
CMD ["python3", "-m", "app.backup", "daemon"]
