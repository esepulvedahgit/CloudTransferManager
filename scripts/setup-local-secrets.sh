#!/usr/bin/env bash
set -euo pipefail

secret_dir="$(dirname "${BASH_SOURCE[0]}")/../secrets"
cert_dir="$(dirname "${BASH_SOURCE[0]}")/../certs"
mkdir -p "$secret_dir"
chmod 700 "$secret_dir"
mkdir -p "$cert_dir"
chmod 700 "$cert_dir"

for name in app_secret_key postgres_password master_key smtp_password; do
  if [[ -e "$secret_dir/$name" ]]; then
    printf 'Secret already exists; leaving it untouched: %s\n' "$secret_dir/$name" >&2
    exit 1
  fi
done

umask 077
openssl rand -hex 48 > "$secret_dir/app_secret_key"
openssl rand -hex 24 > "$secret_dir/postgres_password"
openssl rand -out "$secret_dir/master_key" 32
openssl rand -hex 24 > "$secret_dir/smtp_password"
chmod 600 "$secret_dir"/*
if [[ ! -e "$cert_dir/fullchain.pem" && ! -e "$cert_dir/privkey.pem" ]]; then
  openssl req -x509 -newkey rsa:3072 -nodes -keyout "$cert_dir/privkey.pem" \
    -out "$cert_dir/fullchain.pem" -days 365 \
    -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1'
  chmod 600 "$cert_dir/privkey.pem"
  chmod 644 "$cert_dir/fullchain.pem"
elif [[ ! -e "$cert_dir/fullchain.pem" || ! -e "$cert_dir/privkey.pem" ]]; then
  printf 'TLS certificate and key must either both exist or both be absent.\n' >&2
  exit 1
fi
printf 'Generated local Docker secrets in %s and a self-signed TLS certificate in %s\n' "$secret_dir" "$cert_dir"
