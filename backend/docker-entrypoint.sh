#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Container entrypoint must start as root to stage Docker Secrets safely." >&2
    exit 1
fi
if [ "$#" -eq 0 ]; then
    echo "Container command is required." >&2
    exit 1
fi

source_dir=${CTM_SOURCE_SECRETS_DIR:-/run/secrets}
runtime_dir=${CTM_RUNTIME_SECRETS_DIR:-/run/ctm-secrets}
runtime_uid=${CTM_RUNTIME_UID:-$(id -u ctm)}
runtime_gid=${CTM_RUNTIME_GID:-$(id -g ctm)}
temp_dir=${TRANSFER_TEMP_PATH:-/var/lib/cloud-transfer/tmp}
staging_dir=${TRANSFER_STAGING_PATH:-/var/lib/cloud-transfer/staging}

case "$staging_dir" in
    /*) ;;
    *)
        echo "TRANSFER_STAGING_PATH must be an absolute path." >&2
        exit 1
        ;;
esac
case "$staging_dir" in
    /|/tmp|/var/tmp|/run|/run/*|/dev|/dev/*|/proc|/proc/*|/sys|/sys/*|/etc|/etc/*|/root|/root/*|/home|/home/*|/var/log|/var/log/*|*"/../"*|*/..|*"/./"*|*/.)
        echo "TRANSFER_STAGING_PATH must be a dedicated disk-backed directory." >&2
        exit 1
        ;;
esac

mkdir -p "$runtime_dir" "$temp_dir" "$staging_dir"
chown "$runtime_uid:$runtime_gid" "$runtime_dir" "$temp_dir" "$staging_dir"
chmod 0700 "$runtime_dir" "$temp_dir" "$staging_dir"

for name in app_secret_key postgres_password master_key smtp_password; do
    source_file="$source_dir/$name"
    if [ -f "$source_file" ]; then
        install -o "$runtime_uid" -g "$runtime_gid" -m 0600 \
            "$source_file" "$runtime_dir/$name"
    fi
done

exec gosu "$runtime_uid:$runtime_gid" "$@"
