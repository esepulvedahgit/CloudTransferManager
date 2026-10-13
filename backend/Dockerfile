FROM python:3.12-slim-bookworm

ARG RCLONE_VERSION=1.75.2
ARG TARGETARCH=amd64

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg gosu unzip; \
    curl -fsSLo /tmp/rclone.zip \
        "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}.zip"; \
    curl -fsSLo /tmp/rclone-SHA256SUMS \
        "https://downloads.rclone.org/v${RCLONE_VERSION}/SHA256SUMS"; \
    cd /tmp; \
    grep "rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}.zip" rclone-SHA256SUMS | sha256sum --check --status; \
    unzip -q /tmp/rclone.zip -d /tmp/rclone; \
    install -m 0755 "/tmp/rclone/rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}/rclone" /usr/bin/rclone; \
    test "$(/usr/bin/rclone version | head -n 1)" = "rclone v${RCLONE_VERSION}"; \
    rm -rf /tmp/rclone /tmp/rclone.zip /tmp/rclone-SHA256SUMS; \
    curl -fsSL https://mega.nz/linux/repo/Debian_12/Release.key -o /tmp/mega-release.key; \
    gpg --dearmor --yes -o /usr/share/keyrings/meganz-archive-keyring.gpg /tmp/mega-release.key; \
    rm /tmp/mega-release.key; \
    echo "deb [signed-by=/usr/share/keyrings/meganz-archive-keyring.gpg] https://mega.nz/linux/repo/Debian_12/ ./" \
        > /etc/apt/sources.list.d/meganz.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends megacmd; \
    test -x /usr/bin/mega-version; \
    test -x /usr/bin/mega-cmd; \
    /usr/bin/mega-version; \
    rm -rf /var/lib/apt/lists/*; \
    useradd --uid 10001 --user-group --create-home --home-dir /app --shell /usr/sbin/nologin ctm

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY docker-entrypoint.sh /usr/local/bin/ctm-entrypoint
RUN mkdir -p /var/lib/cloud-transfer/tmp /var/lib/cloud-transfer/staging /var/log/cloud-transfer \
    && chmod 0755 /usr/local/bin/ctm-entrypoint \
    && chown -R ctm:ctm /app /var/lib/cloud-transfer /var/log/cloud-transfer \
    && chmod 0700 /var/lib/cloud-transfer/staging

# The entrypoint needs root only to stage file-backed Docker Secrets (0600), then it
# immediately drops privileges to ctm before starting the application or worker.
#trivy:ignore:DS-0002
USER root
ENTRYPOINT ["/usr/local/bin/ctm-entrypoint"]
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
HEALTHCHECK --interval=20s --timeout=5s --retries=5 --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)"
