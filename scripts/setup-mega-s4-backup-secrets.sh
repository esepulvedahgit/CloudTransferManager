#!/usr/bin/env bash
set -euo pipefail

secret_dir="$(dirname "${BASH_SOURCE[0]}")/../secrets"
restore_dir="$(dirname "${BASH_SOURCE[0]}")/../restore"
mkdir -p "$secret_dir"
chmod 700 "$secret_dir"
mkdir -p "$restore_dir"
chmod 700 "$restore_dir"

create_placeholder() {
  local name="$1"
  local path="$secret_dir/$name"
  if [[ -e "$path" ]]; then
    printf 'Keeping existing backup secret file: %s\n' "$name"
    return
  fi
  (umask 077; : > "$path")
  chmod 600 "$path"
  printf 'Created empty MEGA S4 credential file: %s\n' "$name"
}

create_placeholder mega_s4_access_key
create_placeholder mega_s4_secret_key

key_path="$secret_dir/backup_encryption_key"
if [[ -e "$key_path" ]]; then
  printf 'Keeping existing backup encryption key. Do not regenerate it.\n'
else
  (umask 077; openssl rand -out "$key_path" 32)
  chmod 600 "$key_path"
  printf 'Generated client-side backup encryption key. Store an offline copy before enabling backups.\n'
fi

printf 'Add the MEGA S4 access key ID and secret to their files, then configure the endpoint and bucket in .env.\n'
