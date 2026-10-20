# Especificación 07: Infraestructura, Docker Compose y Seguridad

## 1. Contexto y Objetivos
Definir la topología de contenedores, la gestión de volúmenes persistentes, los secretos de producción y las prácticas de endurecimiento (*hardening*) para ejecutar Cloud Transfer Manager dentro de una red privada.

---

## 2. Topología de Servicios en Docker Compose

```
                              [ Red LAN ]
                                   |
                                   v
                    Host :8080 / :8443 en 0.0.0.0
                                   |
                         +--------------------+
                         |   reverse-proxy    | (Nginx)
                         +--------------------+
                           |                |
              ctm_proxy_edge          ctm_internal
                (bridge)          (bridge, internal=true)
                                          |
        +-------------------------------+-------------------------------+
        |                               |                               |
        v :8000                         v :8080                         |
+---------------+               +---------------+                       |
|    backend    |               |   frontend    |                       |
|   (FastAPI)   |               | (Nginx/React) |                       |
+---------------+               +---------------+                       |
        |                               |                               |
        +---------------+---------------+                               |
                        |                                               |
        +---------------+---------------+                               |
        |                               |                               v
        v :5432                         v :6379                 +---------------+
+---------------+               +---------------+       +---------------+   +---------------+
|   postgres    |               |     redis     | <-----| control-worker|   |     beat      |
+---------------+               +---------------+       +---------------+   +---------------+
                                          ^                       |
                                          +-------- worker --------+
```

El servicio `evaluation-worker` consume la cola Celery `evaluations` con concurrencia uno. Está conectado a `ctm_internal` y `provider_egress`, necesita PostgreSQL, Redis y `master_key` para consultar proveedores, y usa almacenamiento efímero privado para sus configuraciones. No publica puertos y tiene límites configurables de CPU y memoria, además del límite de procesos. El `control-worker` reserva evaluaciones y las publica; no recibe `master_key` ni accede a proveedores.

### Invariante de Red:
- Uvicorn usa explícitamente `--loop asyncio`: MEGAcmd con login por stdin se bloquea bajo uvloop en este despliegue. No dejar la selección del event loop en `auto`.
- Nginx enruta exactamente `/api/v1/transfers` al backend para GET/POST, sin agregar una barra ni redirigir. El prefijo `/api/v1/transfers/` conserva el streaming SSE sin buffering. La regresión se verifica con `docker compose exec -T --user ctm backend python < scripts/check_transfer_proxy.py`.
- El reverse proxy usa el resolver DNS de Docker con resolución dinámica para backend y frontend; al recrear esos contenedores, Nginx actualiza sus IP sin reiniciar el proxy.
- La imagen del worker incluye también Rclone para Google Drive.
- Solo el servicio `reverse-proxy` expone puertos al exterior del host Docker; estos se publican explícitamente sobre `0.0.0.0` para acceso desde otros equipos de la red.
- `postgres`, `redis`, `backend`, `worker`, `control-worker`, `evaluation-worker`, `notification-worker` y `beat` residen en `ctm_internal` sin mapeo de puertos al host. Los workers de transferencias y evaluación conservan salida hacia proveedores; los workers de control solo necesitan PostgreSQL y Redis.
- `control-worker` consume la cola `control` y despacha reservas persistidas; `beat` solicita el despacho cada 15 segundos. Mantener una sola instancia de Beat por despliegue. Un advisory lock de PostgreSQL protege el despacho aunque coincidan una petición HTTP y un tick de Beat.
- `reverse-proxy` también se conecta a la red dedicada `ctm_proxy_edge` (bridge no interno). Docker Engine 29 no materializa los puertos publicados para contenedores conectados únicamente a una red `internal`; mantener `ctm_internal` aislada evita dar salida externa a los servicios privados.
- `0.0.0.0` escucha en todas las interfaces del host, no restringe el origen a la LAN. La restricción LAN-only depende del firewall del host y de no configurar reenvío de esos puertos en el router.
- El frontend usa `nginxinc/nginx-unprivileged`, escucha en `8080` y ejecuta Nginx como UID `101`; el reverse proxy y el healthcheck apuntan a ese puerto.
- La imagen del backend define un healthcheck HTTP predeterminado; Compose lo reemplaza para `backend` y lo desactiva en `worker`, que no expone HTTP.
- Los trabajos programados requieren los servicios `beat` y `control-worker`; ambos son internos y no publican puertos.
- El servicio opcional `backup` usa el perfil Compose `backup`; conecta a PostgreSQL por `ctm_internal` y a MEGA S4 por `provider_egress`, sin puertos publicados. Hace snapshots lógicos cifrados con AES-GCM cada 10 minutos. El volumen `backup_state` guarda solo el estado del último ciclo. Dumps, secretos temporales y paquetes residen en `/run/ctm-backup-work`, un `tmpfs` privado cuyo tamaño (`BACKUP_TMPFS_SIZE`) y límite de memoria (`BACKUP_MEMORY_LIMIT`) se dimensionan para el tamaño de PostgreSQL. La clave de cifrado del respaldo se mantiene aparte de `master_key` y requiere una copia fuera del host.
- El servicio de backup no se inicia en `docker compose up` normal. Habilítalo solo tras provisionar bucket, endpoint, región y archivos locales de credenciales. La salud marca como atrasado un respaldo de más de 15 minutos; este umbral debe probarse con el tamaño real de PostgreSQL antes de afirmar el RPO.
- La prueba de restauración usa el archivo aislado `compose.restore.yaml`, un proyecto Compose y un volumen PostgreSQL separados. Mantener workers detenidos hasta reconciliar trabajos cuyo estado quedó anterior al punto restaurado. El runbook está en `docs/backup-recovery.md`.

---

## 3. Configuración de Secretos (Docker Secrets)

Para evitar almacenar credenciales sensibles en variables de entorno, Compose monta los secretos como archivos en `/run/secrets/`. Los entrypoints del backend, los workers y el respaldo empiezan brevemente como root para copiarlos a un `tmpfs` privado en `/run/ctm-secrets/`, con permisos `0600`, y preparar sus directorios runtime; luego bajan privilegios mediante `gosu`. La API y Celery corren como `ctm` (UID `10001`); el respaldo corre como `postgres`. La excepción de Trivy `DS-0002` está limitada a las instrucciones `USER root` de esas dos imágenes y documenta el bootstrap requerido. Esto evita depender de remapeos de UID/GID que Compose no ofrece para secretos respaldados por archivos locales.
- `master_key`: Clave simétrica de 256 bits para cifrar las credenciales de proveedores.
- `postgres_password`: Contraseña de la base de datos PostgreSQL.
- `smtp_password`: Contraseña para autenticar con el servidor de correo.

---

## 4. Volúmenes Persistentes

```yaml
volumes:
  postgres_data:
  redis_data:
  application_logs:
```

- `postgres_data`: datos de PostgreSQL, incluidas credenciales cifradas.
- `redis_data`: persistencia AOF de Redis.
- `application_logs`: logs de aplicación y workers.
- Las configuraciones efímeras de Rclone están bajo `TRANSFER_TEMP_PATH` (por defecto `/var/lib/cloud-transfer/tmp`), montado como `tmpfs` de 64 MiB en backend y worker. Se crea un archivo por invocación, modo `0600`, que se elimina incluso tras error o cancelación.
- `TRANSFER_STAGING_PATH` (por defecto `/var/lib/cloud-transfer/staging`) es temporal disk-backed dentro del filesystem writable del contenedor, no es volumen ni `tmpfs`. Los adaptadores que lo requieren usan un archivo a la vez y limpian su directorio al terminar cada archivo. Dimensionar el disco Docker para el archivo mayor más la reserva/margen configurados.
- `/run/ctm-secrets` es `tmpfs` efímero para las copias de secretos que crea el entrypoint con modo `0700`; no confundirlo con datos persistentes.

---

## 5. Endpoints de Monitoreo y Salud (Health Checks)

- `GET /health/live`: Verifica que el proceso HTTP de FastAPI responde (liveness).
- `GET /health/ready`: Comprueba dependencias críticas requeridas para operar (readiness):
  - Conexión activa a PostgreSQL (`SELECT 1`).
  - Ping exitoso a Redis (`PING -> PONG`).
  - Presencia y ejecutabilidad del binario Rclone (`rclone version`).
  - Presencia y ejecutabilidad de los clientes MEGAcmd requeridos para login, listado, cuota, descarga, subida y cierre de sesión.
  - Capacidad de agendar tareas en workers Celery.
- `GET /health/notifications`: Supervisa de forma independiente el consumidor de correo. Su estado no retira la API de transferencias de servicio porque las notificaciones son opcionales.
- `GET /health/evaluations`: Supervisa por separado al consumidor de la cola `evaluations`. Si no está disponible, indica que las evaluaciones nuevas están en espera, sin declarar caída una copia que ya se ejecuta ni degradar `/health/ready`.

---

## 6. Variables de Entorno (`.env.example`)

```ini
APP_ENV=production
APP_BASE_URL=https://cloud-transfer.intranet
HTTP_PORT=8080
HTTPS_PORT=8443
APP_SECRET_KEY_FILE=/run/ctm-secrets/app_secret_key
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=cloud_transfer
POSTGRES_USER=cloud_transfer
POSTGRES_PASSWORD_FILE=/run/ctm-secrets/postgres_password
REDIS_URL=redis://redis:6379/0
RCLONE_BINARY=/usr/bin/rclone
MEGACMD_BINARY_DIR=/usr/bin
SMB_ALLOWED_TARGETS=
SMB_INTERACTIVE_TIMEOUT_SECONDS=30
SMB_TRANSFER_MAX_FILES=100000
TRANSFER_MAX_CONCURRENT=1
TRANSFER_MAX_FILES=5000
TRANSFER_MAX_FILE_BYTES=0
TRANSFER_STAGING_RESERVE_BYTES=1073741824
TRANSFER_STAGING_MARGIN_PERCENT=20
TRANSFER_TEMP_PATH=/var/lib/cloud-transfer/tmp
TRANSFER_STAGING_PATH=/var/lib/cloud-transfer/staging
TRANSFER_MAX_RETRIES=5
EVALUATION_WORKER_CPU_LIMIT=1.00
EVALUATION_WORKER_MEMORY_LIMIT=768M
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD_FILE=/run/ctm-secrets/smtp_password
SMTP_USE_TLS=true
SMTP_FROM_ADDRESS=
SESSION_COOKIE_SECURE=true
SESSION_MAX_AGE_SECONDS=28800
```

Los archivos reales se generan con `scripts/setup-local-secrets.sh`; este ejemplo no contiene sus valores. Compose suministra `MASTER_ENCRYPTION_KEY_FILE=/run/ctm-secrets/master_key` al backend y a los workers que necesitan descifrar credenciales. El catálogo completo y las reglas de cambio de rutas/temporales están en `README.md`.

### Despliegue de la corrección de transferencias

Respaldar PostgreSQL y etiquetar imágenes de contenedores actuales antes de reconstruir. Detener Beat/control-worker y suspender consumo de nuevas transferencias; esperar a que no haya trabajos activos antes de recrear. Construir backend, worker, beat, control-worker, notification-worker y frontend. Recrear con `--no-deps` backend, worker, notification-worker y frontend; conservar PostgreSQL, Redis y volúmenes. Verificar readiness, proxy, identidad de imágenes y copia real acotada antes de levantar Beat/control-worker. Ante fallo crítico, dejar despacho detenido y recrear usando las etiquetas guardadas (no restaurar la BD sobre datos nuevos).
