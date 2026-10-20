#!/usr/bin/env bash
set -euo pipefail
umask 077

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }

for command_name in docker openssl curl; do
  command -v "$command_name" >/dev/null 2>&1 || die "Falta $command_name en PATH."
done
docker compose version >/dev/null || die 'Docker Compose v2 no está disponible.'
docker info >/dev/null || die 'No se puede conectar al daemon de Docker.'
[[ -f .env.example && -f compose.yaml ]] || die 'Ejecuta el script desde un checkout completo del proyecto.'

read -r -p 'Dominio DNS de la aplicación (ej. transferencias.intranet.local): ' domain
domain="${domain,,}"
[[ ${#domain} -le 253 && ${#domain} -ge 3 ]] || die 'Dominio inválido.'
[[ "$domain" == *.* ]] || die 'Introduce un dominio DNS completo.'
IFS='.' read -r -a labels <<< "$domain"
for label in "${labels[@]}"; do
  [[ ${#label} -ge 1 && ${#label} -le 63 && "$label" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || die 'Dominio inválido.'
done
[[ "$domain" != *. ]] || die 'Dominio inválido.'

url="https://$domain"
https_port=443
fresh_install=true
if [[ -e .env ]]; then
  [[ -f .env && ! -L .env ]] || die '.env debe ser un archivo normal.'
  configured_url="$(sed -n 's/^APP_BASE_URL=//p' .env | tail -n 1)"
  [[ "$configured_url" =~ ^https://([^/:]+)(:([0-9]+))?/?$ ]] || die 'APP_BASE_URL de .env debe ser una URL HTTPS sin ruta.'
  [[ "${BASH_REMATCH[1],,}" == "$domain" ]] || die 'El dominio indicado no coincide con APP_BASE_URL de .env; no se cambiarán claves ni certificado.'
  url="$configured_url"
  https_port="${BASH_REMATCH[3]:-443}"
  fresh_install=false
fi

for directory in secrets certs; do
  [[ ! -L "$directory" ]] || die "$directory no puede ser un enlace simbólico."
  mkdir -p "$directory"
  chmod 700 "$directory"
done

required_secrets=(app_secret_key postgres_password master_key smtp_password)
existing=0
for secret_name in "${required_secrets[@]}"; do
  secret_path="secrets/$secret_name"
  [[ ! -L "$secret_path" ]] || die "$secret_path no puede ser un enlace simbólico."
  if [[ -e "$secret_path" ]]; then
    [[ -f "$secret_path" && -s "$secret_path" ]] || die "$secret_path está vacío o no es un archivo normal."
    ((existing+=1))
  fi
done
[[ $existing -eq 0 || $existing -eq ${#required_secrets[@]} ]] || die 'El conjunto de secretos está incompleto; se preservó para su recuperación.'
if [[ "$fresh_install" == false && $existing -eq 0 ]]; then
  die 'Ya existe .env pero faltan todos los secretos. No se regenerarán claves de una instalación existente.'
fi
if [[ $existing -eq ${#required_secrets[@]} ]]; then
  [[ $(wc -c < secrets/master_key) -eq 32 ]] || die 'master_key debe contener exactamente 32 bytes.'
  chmod 600 secrets/app_secret_key secrets/postgres_password secrets/master_key secrets/smtp_password
fi

cert_path=certs/fullchain.pem
key_path=certs/privkey.pem
[[ ! -L "$cert_path" && ! -L "$key_path" ]] || die 'El certificado y la clave TLS no pueden ser enlaces simbólicos.'
if [[ -e "$cert_path" || -e "$key_path" ]]; then
  [[ -f "$cert_path" && -f "$key_path" ]] || die 'El certificado TLS y su clave deben existir juntos.'
  openssl x509 -in "$cert_path" -noout -checkhost "$domain" | grep -q 'does match' || die 'El certificado existente no corresponde al dominio.'
  openssl x509 -in "$cert_path" -noout -checkend 0 >/dev/null || die 'El certificado existente está vencido.'
  openssl x509 -in "$cert_path" -noout -ext subjectAltName | grep -Fq "DNS:$domain" || die 'El certificado existente no incluye el dominio en SAN.'
  cmp -s <(openssl x509 -in "$cert_path" -pubkey -noout | openssl pkey -pubin -outform DER 2>/dev/null) \
    <(openssl pkey -in "$key_path" -pubout -outform DER 2>/dev/null) || die 'La clave TLS no corresponde al certificado.'
  chmod 600 "$key_path"
  info 'Se conservará el certificado TLS existente.'
else
  cert_tmp="$(mktemp certs/.fullchain.XXXXXX)"
  key_tmp="$(mktemp certs/.privkey.XXXXXX)"
  trap 'rm -f "$cert_tmp" "$key_tmp"' EXIT
  openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 365 \
    -keyout "$key_tmp" -out "$cert_tmp" -subj "/CN=$domain" \
    -addext "subjectAltName=DNS:$domain" >/dev/null 2>&1
  chmod 600 "$key_tmp"
  chmod 644 "$cert_tmp"
  mv "$key_tmp" "$key_path"
  mv "$cert_tmp" "$cert_path"
  trap - EXIT
  info "Certificado autofirmado creado para $domain (365 días)."
fi

if [[ $existing -eq 0 ]]; then
  bash scripts/setup-local-secrets.sh
else
  info 'Se conservarán los secretos existentes.'
fi

if [[ "$fresh_install" == false ]] && grep -Eq '^BACKUP_ENABLED=(true|1)$' .env; then
  for secret_name in backup_encryption_key mega_s4_access_key mega_s4_secret_key; do
    [[ -s "secrets/$secret_name" ]] || die "El respaldo está activo y falta secrets/$secret_name."
  done
fi
for secret_name in backup_encryption_key mega_s4_access_key mega_s4_secret_key; do
  [[ ! -L "secrets/$secret_name" ]] || die "secrets/$secret_name no puede ser un enlace simbólico."
done
bash scripts/setup-mega-s4-backup-secrets.sh
[[ $(wc -c < secrets/backup_encryption_key) -eq 32 ]] || die 'backup_encryption_key debe contener exactamente 32 bytes.'
chmod 600 secrets/backup_encryption_key secrets/mega_s4_access_key secrets/mega_s4_secret_key

if [[ "$fresh_install" == true ]]; then
  env_tmp="$(mktemp .env.XXXXXX)"
  trap 'rm -f "$env_tmp"' EXIT
  sed -e "s|^APP_BASE_URL=.*$|APP_BASE_URL=$url|" \
      -e 's|^HTTP_PORT=.*$|HTTP_PORT=80|' \
      -e 's|^HTTPS_PORT=.*$|HTTPS_PORT=443|' .env.example > "$env_tmp"
  chmod 600 "$env_tmp"
  mv "$env_tmp" .env
  trap - EXIT
  info 'Configuración .env creada con HTTPS en el puerto 443.'
fi

docker compose config --quiet
info 'Construyendo e iniciando servicios; esta operación puede tardar varios minutos.'
docker compose up -d --build --wait --wait-timeout 300
curl --fail --silent --show-error --noproxy '*' --cacert "$cert_path" \
  --resolve "$domain:$https_port:127.0.0.1" "$url/health/ready" >/dev/null \
  || die 'Compose inició, pero el endpoint /health/ready no respondió correctamente.'

info "Despliegue listo: $url"
info "Configura DNS (o hosts en cada cliente) para que $domain apunte a este servidor."
info 'Distribuye el certificado certs/fullchain.pem a los clientes que deban confiar en él.'
if [[ "$fresh_install" == true && -t 0 && -t 1 ]]; then
  read -r -p "Correo del administrador inicial [admin@$domain]: " admin_email
  admin_email="${admin_email:-admin@$domain}"
  [[ "$admin_email" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die 'Correo del administrador inválido; crea el usuario con la CLI.'
  info 'La CLI solicitará una contraseña inicial de al menos 12 caracteres.'
  docker compose exec backend python -m app.cli create-admin --username admin --email "$admin_email"
else
  info 'Crea el administrador inicial con: docker compose exec backend python -m app.cli create-admin --username TU_USUARIO --email TU_CORREO'
fi
info 'Guarda una copia externa y protegida de secrets/master_key y, antes de activar respaldos, de secrets/backup_encryption_key.'
