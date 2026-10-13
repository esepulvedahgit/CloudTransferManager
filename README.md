# Cloud Transfer Manager

Aplicación web privada para explorar y copiar archivos entre MEGA, Google Drive, Seafile y recursos SMB privados. Las credenciales se cifran en el backend; tanto la navegación/prueba de conexión como las transferencias usan Rclone mediante argumentos separados y configuraciones temporales con permisos `0600`. El worker ejecuta los trabajos de copia; solo se permite `COPY`.

## Inicio local con Docker

Requiere Docker Compose v2 y OpenSSL. La primera ejecución crea secretos aleatorios locales y un certificado TLS autofirmado para desarrollo:

```bash
cp .env.example .env
./scripts/setup-local-secrets.sh
docker compose up -d --build
```

El reverse proxy publica explícitamente en `0.0.0.0` los puertos configurados (`HTTP_PORT=8080` y `HTTPS_PORT=8443` por defecto). Abre `https://localhost:8443` localmente o `https://<IP-LAN-del-VPS>:8443` desde otro equipo de la LAN. En el firewall, permite TCP/8080 y TCP/8443 solo desde la subred LAN y no reenvíes esos puertos desde el router a Internet: `0.0.0.0` escucha en todas las interfaces y por sí solo no limita el origen. Acepta la advertencia del certificado autofirmado. Antes de producción, reemplaza el certificado de `certs/` por uno confiable y revisa `.env` y los secretos montados. El proxy redirige HTTP a HTTPS; es el único servicio con puertos publicados. Se conecta a `ctm_internal` para llegar al frontend/backend y a `ctm_proxy_edge`, una red bridge dedicada requerida para publicar puertos con Docker Engine 29. PostgreSQL, Redis, backend y worker permanecen sin puertos publicados en redes privadas.

Para usar OAuth localmente, cambia `APP_BASE_URL` en `.env` a `https://localhost:8443` y registra en Google Cloud `https://localhost:8443/api/v1/connections/google-oauth/callback` como URI de redirección.

No hay usuarios ni contraseñas predeterminados. Crea el primer administrador y define la contraseña de forma interactiva:

```bash
docker compose exec backend python -m app.cli create-admin \
  --username admin --email admin@intranet.local
```

El usuario deberá cambiar la contraseña inicial al entrar. Después, un administrador puede configurar conexiones MEGA/Google Drive, crear usuarios y asignar accesos a conexiones privadas desde la interfaz.

Google Drive requiere un cliente OAuth web configurado en Google Cloud con URI de redirección `${APP_BASE_URL}/api/v1/connections/google-oauth/callback`. En la interfaz se ingresan el `client_id` y `client_secret`; la autorización y el intercambio del código ocurren en el servidor, y los tokens se cifran antes de guardarlos. La conexión queda inactiva hasta superar la prueba. MEGA requiere usuario y contraseña; el API nunca devuelve credenciales al frontend.

Para migrar un NAS, configura primero el acceso de red del host Docker y `SMB_ALLOWED_TARGETS`; el worker y el backend deben alcanzar el NAS por la LAN o por una VPN externa ya disponible. CTM no administra la VPN. El acceso SMB se configura desde **Servicios privados**, separado de las conexiones cloud. Consulta [la guía de conexión y migración SMB](docs/integrations/smb-nas-setup.md) para rutas, firewall, límites y reanudación de transferencias.

## Verificación

Backend, desde `backend/`:

```bash
.venv/bin/pytest -v --tb=short
.venv/bin/ruff check app tests
.venv/bin/mypy app
```

Frontend, desde `frontend/`:

```bash
npm ci
npm test
npm run lint
npm run build
```

El vertical slice de desarrollo usa `MemoryStore` y `MockStorageProvider`. En `APP_ENV=production`, el API usa PostgreSQL, los trabajos se envían a Celery/Redis y el worker ejecuta el adaptador mock o Rclone según el tipo de conexión. Las migraciones se aplican al iniciar el backend.

La creación manual de carpetas está fuera del MVP para mantener Rclone dentro de la lista blanca de comandos no destructivos.
# CloudTransferManager
