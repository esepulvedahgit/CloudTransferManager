# Especificación 07: Infraestructura, Docker Compose y Seguridad

## 1. Contexto y Objetivos
Definir la topología de contenedores, la gestión de volúmenes persistentes, los secretos de producción y las prácticas de endurecimiento (*hardening*) para ejecutar Cloud Transfer Manager dentro de una red privada.

---

## 2. Topología de Servicios en Docker Compose

```
                              [ Red Externa / LAN ]
                                        |
                                        v :80 / :443
                             +--------------------+
                             |   reverse-proxy    | (Nginx)
                             +--------------------+
                                        |
                   [ Red Interna Docker: ctm_internal (bridge) ]
                                        |
        +-------------------------------+-------------------------------+
        |                               |                               |
        v :8000                         v :80                           |
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
+---------------+               +---------------+               |    worker     |
|   postgres    |               |     redis     | <-----------+ | (Celery+Rclone|
+---------------+               +---------------+               +---------------+
```

### Invariante de Red:
- Solo el servicio `reverse-proxy` expone puertos al exterior del host Docker.
- `postgres`, `redis`, `backend` y `worker` residen en la red interna `ctm_internal` sin mapeo de puertos al host.

---

## 3. Configuración de Secretos (Docker Secrets)

Para evitar almacenar credenciales sensibles en variables de entorno, se utilizan secretos montados como archivos en `/run/secrets/`:
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
  temporary_transfer_data:
    driver: local
```
*El directorio `/var/lib/cloud-transfer/tmp` dentro del worker se monta sobre `temporary_transfer_data` para almacenar buffers temporales y archivos de configuración efímeros.*

---

## 5. Endpoints de Monitoreo y Salud (Health Checks)

- `GET /health/live`: Verifica que el proceso HTTP de FastAPI responde (liveness).
- `GET /health/ready`: Comprueba dependencias críticas requeridas para operar (readiness):
  - Conexión activa a PostgreSQL (`SELECT 1`).
  - Ping exitoso a Redis (`PING -> PONG`).
  - Presencia y ejecutabilidad del binario Rclone (`rclone version`).
  - Capacidad de agendar tareas en workers Celery.

---

## 6. Variables de Entorno (`.env.example`)

```ini
APP_ENV=production
APP_BASE_URL=https://cloud-transfer.intranet
APP_SECRET_KEY=cambiar_en_produccion_clave_secreta_de_sesiones_64_caracteres
MASTER_ENCRYPTION_KEY_FILE=/run/secrets/master_key
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=cloud_transfer
POSTGRES_USER=cloud_transfer
POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password
REDIS_URL=redis://redis:6379/0
RCLONE_BINARY=/usr/bin/rclone
TRANSFER_MAX_CONCURRENT=2
TRANSFER_TEMP_PATH=/var/lib/cloud-transfer/tmp
TRANSFER_MAX_RETRIES=5
SMTP_HOST=smtp.intranet.local
SMTP_PORT=587
SMTP_USERNAME=notificaciones@intranet.local
SMTP_PASSWORD_FILE=/run/secrets/smtp_password
SMTP_USE_TLS=true
SMTP_FROM_ADDRESS=notificaciones@intranet.local
SESSION_COOKIE_SECURE=true
SESSION_MAX_AGE_SECONDS=28800
```
