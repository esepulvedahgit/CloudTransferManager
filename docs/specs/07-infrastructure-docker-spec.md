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

### Invariante de Red:
- Uvicorn usa explícitamente `--loop asyncio`: MEGAcmd con login por stdin se bloquea bajo uvloop en este despliegue. No dejar la selección del event loop en `auto`.
- Nginx enruta exactamente `/api/v1/transfers` al backend para GET/POST, sin agregar una barra ni redirigir. El prefijo `/api/v1/transfers/` conserva el streaming SSE sin buffering. La regresión se verifica con `docker compose exec -T --user ctm backend python < scripts/check_transfer_proxy.py`.
- El reverse proxy usa el resolver DNS de Docker con resolución dinámica para backend y frontend; al recrear esos contenedores, Nginx actualiza sus IP sin reiniciar el proxy.
- La imagen del worker incluye también Rclone para Google Drive.
- Solo el servicio `reverse-proxy` expone puertos al exterior del host Docker; estos se publican explícitamente sobre `0.0.0.0` para acceso desde otros equipos de la red.
- `postgres`, `redis`, `backend`, `worker`, `control-worker` y `beat` residen en `ctm_internal` sin mapeo de puertos al host. El worker de transferencias conserva salida hacia proveedores; el worker de control solo necesita PostgreSQL y Redis.
- `control-worker` consume la cola `control` y despacha reservas persistidas; `beat` solicita el despacho cada 15 segundos. Mantener una sola instancia de Beat por despliegue. Un advisory lock de PostgreSQL protege el despacho aunque coincidan una petición HTTP y un tick de Beat.
- `reverse-proxy` también se conecta a la red dedicada `ctm_proxy_edge` (bridge no interno). Docker Engine 29 no materializa los puertos publicados para contenedores conectados únicamente a una red `internal`; mantener `ctm_internal` aislada evita dar salida externa a los servicios privados.
- `0.0.0.0` escucha en todas las interfaces del host, no restringe el origen a la LAN. La restricción LAN-only depende del firewall del host y de no configurar reenvío de esos puertos en el router.
- El frontend usa `nginxinc/nginx-unprivileged`, escucha en `8080` y ejecuta Nginx como UID `101`; el reverse proxy y el healthcheck apuntan a ese puerto.
- La imagen del backend define un healthcheck HTTP predeterminado; Compose lo reemplaza para `backend` y lo desactiva en `worker`, que no expone HTTP.
- Los trabajos programados requieren los servicios `beat` y `control-worker`; ambos son internos y no publican puertos.

---

## 3. Configuración de Secretos (Docker Secrets)

Para evitar almacenar credenciales sensibles en variables de entorno, Compose monta los secretos como archivos en `/run/secrets/`. El entrypoint del backend y worker, iniciado brevemente como root, los copia a un `tmpfs` privado en `/run/ctm-secrets/`, con propietario `ctm` y permisos `0600`; después baja privilegios mediante `gosu` antes de ejecutar la aplicación. El proceso de aplicación y Celery corren como `ctm` (UID `10001`). La excepción de Trivy `DS-0002` está limitada a esta instrucción `USER root` y documenta ese bootstrap requerido. Esto evita depender de remapeos de UID/GID que Compose no ofrece para secretos respaldados por archivos locales.
- `master_key`: Clave simétrica de 256 bits para cifrar las credenciales de proveedores.
- `postgres_password`: Contraseña de la base de datos PostgreSQL.
- `smtp_password`: Contraseña para autenticar con el servidor de correo.

---

## 4. Volúmenes Persistentes

```yaml
volumes:
  postgres_data:
    driver: local
  application_logs:
    driver: local
```
*Los archivos de configuración Rclone se guardan en un `tmpfs` privado por contenedor y se eliminan al terminar cada invocación. El staging MEGA↔Drive es configurable, disk-backed y no persistente; cada directorio temporal por archivo se elimina antes de procesar el siguiente.*

---

## 5. Endpoints de Monitoreo y Salud (Health Checks)

- `GET /health/live`: Verifica que el proceso HTTP de FastAPI responde (liveness).
- `GET /health/ready`: Comprueba dependencias críticas requeridas para operar (readiness):
  - Conexión activa a PostgreSQL (`SELECT 1`).
  - Ping exitoso a Redis (`PING -> PONG`).
  - Presencia y ejecutabilidad del binario Rclone (`rclone version`).
  - Presencia y ejecutabilidad de los clientes MEGAcmd requeridos para login, listado, cuota, descarga, subida y cierre de sesión.
  - Capacidad de agendar tareas en workers Celery.

---

## 6. Variables de Entorno (`.env.example`)

```ini
APP_ENV=production
APP_BASE_URL=https://cloud-transfer.intranet
APP_SECRET_KEY=cambiar_en_produccion_clave_secreta_de_sesiones_64_caracteres
MASTER_ENCRYPTION_KEY_FILE=/run/ctm-secrets/master_key
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=cloud_transfer
POSTGRES_USER=cloud_transfer
POSTGRES_PASSWORD_FILE=/run/ctm-secrets/postgres_password
REDIS_URL=redis://redis:6379/0
RCLONE_BINARY=/usr/bin/rclone
MEGACMD_BINARY_DIR=/usr/bin
TRANSFER_MAX_CONCURRENT=1
TRANSFER_MAX_FILES=5000
TRANSFER_MAX_FILE_BYTES=0
TRANSFER_STAGING_RESERVE_BYTES=1073741824
TRANSFER_STAGING_MARGIN_PERCENT=20
TRANSFER_TEMP_PATH=/var/lib/cloud-transfer/tmp
TRANSFER_STAGING_PATH=/var/lib/cloud-transfer/staging
TRANSFER_MAX_RETRIES=5
SMTP_HOST=smtp.intranet.local
SMTP_PORT=587
SMTP_USERNAME=notificaciones@intranet.local
SMTP_PASSWORD_FILE=/run/ctm-secrets/smtp_password
SMTP_USE_TLS=true
SMTP_FROM_ADDRESS=notificaciones@intranet.local
SESSION_COOKIE_SECURE=true
SESSION_MAX_AGE_SECONDS=28800
```
