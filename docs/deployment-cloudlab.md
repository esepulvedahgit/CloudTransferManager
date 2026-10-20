# Despliegue local CloudLab

Verificado el 2026-10-14 desde el servidor Docker.

- URL: https://transfer.cloudlab.cl
- Resolución observada en el servidor: `192.168.1.8`.
- `.env`: `APP_BASE_URL=https://transfer.cloudlab.cl`, `HTTP_PORT=80`, `HTTPS_PORT=443`, `SESSION_COOKIE_SECURE=true`.
- Certificado autofirmado RSA 3072, SAN `DNS:transfer.cloudlab.cl`, válido hasta el 2027-10-14 (UTC). Archivos locales: `certs/fullchain.pem` y `certs/privkey.pem`.
- Secretos generados mediante `bash scripts/setup-local-secrets.sh`, excluidos de Git junto con `.env` y `certs/`.

Los equipos clientes deben resolver el dominio a `192.168.1.8`, mediante DNS local o una entrada en su archivo hosts. Para confiar en TLS, importar únicamente el certificado público `certs/fullchain.pem` en el almacén de confianza correspondiente. Nunca distribuir la clave privada.

## Operación

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 backend worker control-worker beat
curl --noproxy '*' --cacert certs/fullchain.pem https://transfer.cloudlab.cl/health/ready
```

No regenerar los secretos de esta instalación. Respaldar PostgreSQL y la clave maestra según el README.

## Evidencia de arranque

- Ocho servicios activos; backend, frontend, PostgreSQL y Redis saludables.
- Solo Nginx publica puertos del host: 80 y 443.
- Frontend HTTP 200; `/health/live` devuelve `alive`; `/health/ready` devuelve `ready`, verificando el certificado con `--cacert`.
- HTTP redirige a `https://transfer.cloudlab.cl/` con 301.
- Ambos workers Celery responden `pong`; Beat despacha y control-worker procesa la revisión de pendientes cada 15 segundos.
- Alembic: `20261013_0009 (head)`.
- `scripts/check_compose_network.py` aprobado.
- `scripts/check_transfer_proxy.py` aprobado: GET y POST sin autenticación llegan a la API con 401 y sin redirección.

## Pendientes y límites

El administrador inicial debe crearse con un usuario y correo elegidos para esta instalación:

```bash
docker compose exec backend python -m app.cli create-admin --username USUARIO --email CORREO
```

El CLI solicita una contraseña y exige cambiarla al primer ingreso. No existen credenciales predeterminadas.

No se probaron proveedores externos, SMTP, transferencias reales ni la suite completa. Durante la construcción, `npm ci` reportó tres vulnerabilidades (una moderada y dos críticas); falta analizar sus dependencias y alcance. La compilación del frontend terminó correctamente.
