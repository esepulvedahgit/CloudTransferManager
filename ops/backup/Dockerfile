FROM postgres:16-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates gosu python3 python3-boto3 python3-cryptography \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ctm
COPY backend/app/__init__.py ./app/__init__.py
COPY backend/app/backup.py ./app/backup.py
COPY backend/app/backup_artifacts.py ./app/backup_artifacts.py
COPY ops/backup/entrypoint.sh /usr/local/bin/ctm-backup-entrypoint

RUN mkdir -p /var/lib/ctm-backups \
    && chmod 0700 /var/lib/ctm-backups \
    && chmod 0755 /usr/local/bin/ctm-backup-entrypoint

ENV PYTHONPATH=/opt/ctm
ENTRYPOINT ["/usr/local/bin/ctm-backup-entrypoint"]
CMD ["python3", "-m", "app.backup", "daemon"]
