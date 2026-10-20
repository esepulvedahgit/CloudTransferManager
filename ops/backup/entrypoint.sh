#!/bin/sh
set -eu
umask 077

if [ "$(id -u)" -ne 0 ]; then
    echo "backup entrypoint must start as root to stage Docker secrets" >&2
    exit 1
fi

source_secrets_dir=${CTM_BACKUP_SOURCE_SECRETS_DIR:-/run/secrets}
source_env_file=${CTM_BACKUP_SOURCE_ENV_FILE:-/run/ctm-config-source/app.env}
runtime_dir=${CTM_BACKUP_RUNTIME_DIR:-/run/ctm-secrets}
state_dir=${BACKUP_STATE_DIR:-/var/lib/ctm-backups}
work_dir=${BACKUP_WORK_DIR:-${state_dir}/work}
backup_uid=${CTM_BACKUP_UID:-$(id -u postgres)}
backup_gid=${CTM_BACKUP_GID:-$(id -g postgres)}

if [ ! -f "$source_env_file" ] || [ -L "$source_env_file" ]; then
    echo "backup configuration source is missing or invalid" >&2
    exit 1
fi

mkdir -p "$runtime_dir" "$state_dir" "$work_dir"
chmod 0700 "$runtime_dir" "$state_dir" "$work_dir"

for name in \
    app_secret_key \
    master_key \
    postgres_password \
    smtp_password \
    backup_encryption_key \
    mega_s4_access_key \
    mega_s4_secret_key
do
    source_file=${source_secrets_dir}/${name}
    if [ ! -f "$source_file" ] || [ -L "$source_file" ]; then
        echo "required backup secret is missing or invalid" >&2
        exit 1
    fi
    install -m 0600 "$source_file" "${runtime_dir}/${name}"
done
install -m 0600 "$source_env_file" "${runtime_dir}/app.env"

chown "$backup_uid:$backup_gid" "$runtime_dir" "$state_dir" "$work_dir"
chown "$backup_uid:$backup_gid" "$runtime_dir"/*
chmod 0700 "$runtime_dir" "$state_dir" "$work_dir"

if [ "${CTM_BACKUP_RUN_AS_ROOT:-false}" = "true" ]; then
    exec "$@"
fi

exec gosu "$backup_uid:$backup_gid" "$@"
