# Contratos de API REST (Cloud Transfer Manager)

Base URL: `/api/v1`

---

## 1. Autenticación (`/auth`)

Todas las mutaciones requieren el encabezado `X-CSRF-Token`. Obtenerlo con `GET /api/v1/auth/csrf`; la sesión se mantiene en una cookie HttpOnly.

### `GET /api/v1/auth/csrf`
- **Response `200 OK`:** Devuelve `csrf_token` y establece la cookie `ctm_csrf`.

### `POST /api/v1/auth/login`
- **Request Body:**
```json
{
  "username_or_email": "admin",
  "password": "Password123!"
}
```
- **Response `200 OK`:** Establece cookie HttpOnly `ctm_session`.
```json
{
  "id": "e267fe79-086e-4519-a749-5be01e034efe",
  "username": "admin",
  "email": "admin@intranet.local",
  "role": "ADMIN"
}
```
- **Response `401 Unauthorized`:**
```json
{
  "detail": "No fue posible iniciar sesión. Revisa tus datos e inténtalo nuevamente."
}
```

### `POST /api/v1/auth/logout`
- **Response `200 OK`:** Invalida sesión y limpia cookie.

### `GET /api/v1/auth/me`
- **Response `200 OK`:** Devuelve datos del usuario autenticado.

### `POST /api/v1/auth/password`
- **Request Body:** `{ "current_password": "...", "new_password": "..." }` (mínimo 12 caracteres).
- **Response `200 OK`:** `{ "message": "Contraseña actualizada" }`.

---

## 2. Usuarios (`/users`) [Solo ADMIN]

### `GET /api/v1/users`
- **Response `200 OK`:** Lista paginada de usuarios.

### `POST /api/v1/users`
- **Request Body:**
```json
{
  "username": "operador",
  "email": "operador@intranet.local",
  "password": "TemporalPassword123!",
  "role": "USER",
  "must_change_password": true
}
```

### `PATCH /api/v1/users/{user_id}`
- Actualiza `username`, `email`, `role` o `is_active`; admite uno o varios campos.
- No permite desactivar ni degradar al último administrador activo.

### `POST /api/v1/users/{user_id}/password-reset`
- **Request Body:** `{ "password": "..." }`.
- Restablece la contraseña y obliga al usuario a cambiarla en el siguiente inicio de sesión.

### `POST /api/v1/users/{user_id}/deactivate`
- **Response `200 OK`:** Revoca sesiones activas e inhabilita usuario.

---

## 3. Configuración SMTP (`/settings/smtp`) [Solo ADMIN]

Todas las rutas requieren una sesión de administrador. `PUT` y `POST .../test` también requieren `X-CSRF-Token`.

### `GET /api/v1/settings/smtp`
- Devuelve `host`, `port`, `use_tls`, `from_address` y `credentials_configured`.
- Nunca devuelve el usuario SMTP ni la contraseña. Antes del primer guardado muestra los valores no secretos del entorno actual.

### `PUT /api/v1/settings/smtp`
- **Request Body:**
```json
{
  "host": "smtp.example.test",
  "port": 587,
  "use_tls": true,
  "from_address": "notificaciones@example.test",
  "username": "",
  "password": ""
}
```
- `port` debe estar entre 1 y 65535; `host` y `from_address` se validan antes de guardar. Usuario y contraseña se envían juntos o se omiten juntos. Si ambos quedan vacíos, se conservan las credenciales actuales.
- Los errores de validación devuelven `422` con un mensaje genérico; la respuesta nunca repite los valores recibidos, incluidas las credenciales.
- Usuario y contraseña se cifran juntos con AES-256-GCM envelope usando la clave maestra montada desde Docker Secret. Ni la respuesta ni la auditoría contienen sus valores.
- **Response `200 OK`:** devuelve los mismos campos seguros de `GET` y el indicador `credentials_configured`.
- Registra `SMTP_SETTINGS_UPDATED` con una lista de nombres de campos modificados, sin valores sensibles.

### `POST /api/v1/settings/smtp/test`
- Acepta el mismo cuerpo que `PUT` y prueba el borrador sin guardarlo ni enviar un correo. Si se omiten las credenciales, usa las guardadas o las disponibles en el entorno.
- Negocia SMTP y STARTTLS según `use_tls`, y autentica cuando hay credenciales configuradas.
- **Response `200 OK`:** `{ "success": true, "message": "Conexión SMTP correcta" }` o `{ "success": false, "message": "No se pudo conectar con SMTP" }`. Los detalles de la respuesta del servidor no se devuelven.

---

## 4. Conexiones (`/connections`)

`GET /api/v1/connections` devuelve únicamente las conexiones globales o asignadas al usuario. Los campos de credenciales nunca forman parte de las respuestas.
Las conexiones MEGA, Google Drive y Seafile nuevas quedan `INACTIVE` hasta superar la prueba; las simuladas quedan activas. Un administrador puede ejecutar la prueba desde el explorador o mediante el endpoint siguiente.

### `DELETE /api/v1/connections/{connection_id}` [Solo ADMIN]
- Requiere CSRF y archiva la conexión de forma lógica; no elimina la fila ni las referencias de transferencias históricas.
- Antes de responder `204 No Content`, borra las credenciales cifradas y revoca los permisos privados. No realiza operaciones en el proveedor.
- **Errores:** `403 Forbidden` sin rol ADMIN o token CSRF válido; `404 Not Found` si el UUID no existe o ya está archivado; `409 Conflict` si una transferencia asociada está `QUEUED`, `VALIDATING`, `RUNNING`, `RETRYING` o `CANCELLING`.
- Las conexiones archivadas no aparecen en listados y no admiten navegación, pruebas, OAuth, cambios, permisos ni transferencias nuevas.

### `POST /api/v1/connections`
- **Request Body (MEGA):**
```json
{
  "name": "MEGA Corporativo",
  "provider_type": "MEGA",
  "credentials": {
    "username": "usuario@mega.nz",
    "password": "MegaSecretPassword"
  }
}
```
- **Request Body (Google Drive OAuth):**
```json
{
  "name": "Google Drive Proyectos",
  "provider_type": "GOOGLE_DRIVE",
  "credentials": {
    "client_id": "...",
    "client_secret": "...",
    "team_drive": "..."
  }
}
```
Para Google Drive se envían inicialmente `client_id` y `client_secret` (más `team_drive`/`root_folder_id` opcionales); la conexión queda inactiva mientras se completa OAuth.

- **Request Body (Seafile):**
```json
{
  "name": "Seafile CE",
  "provider_type": "SEAFILE",
  "adapter_type": "RCLONE",
  "is_global": true,
  "credentials": {
    "url": "https://seafile.example.com",
    "username": "operador@example.com",
    "password": "contraseña-de-cuenta",
    "library": "Archivos",
    "library_password": "contraseña-de-biblioteca"
  }
}
```
- `adapter_type` acepta `RCLONE` o `SEAFILE_API`; omitirlo equivale a `RCLONE`. La API es una elección explícita por conexión y un error nunca cambia de adaptador.
- `library` y `library_password` son opcionales. Sin biblioteca fija, el explorador muestra las bibliotecas disponibles; la contraseña se usa solo para una biblioteca cifrada.
- La respuesta contiene `adapter_type` y `credentials_configured`, pero nunca credenciales, OTP ni token de Seafile.

### `POST /api/v1/connections/{connection_id}/google-oauth/start` [Solo ADMIN]
- **Response `200 OK`:** `{ "authorization_url": "https://accounts.google.com/..." }`. El estado anti-CSRF queda vinculado a la sesión del administrador; el navegador no recibe el secreto del cliente ni tokens.
- Google debe tener registrada como URI de redirección `${APP_BASE_URL}/api/v1/connections/google-oauth/callback`.

### `GET /api/v1/connections/google-oauth/callback`
- Intercambia el código OAuth en el servidor, cifra los tokens y redirige a la interfaz. La conexión sigue inactiva hasta una prueba exitosa.

### `POST /api/v1/connections/{connection_id}/test`
- **Request Body opcional para Seafile:** `{ "otp": "123456" }`. El OTP se usa únicamente para autenticar esta prueba; no se persiste ni se devuelve. Si se omite, se usa el token cifrado vigente o se intenta autenticar con las credenciales guardadas.
- **Response `200 OK`:**
```json
{
  "success": true,
  "message": "Conexión verificada exitosamente",
  "quota_total_bytes": 21474836480,
  "quota_used_bytes": 5368709120
}
```

### `PATCH /api/v1/connections/{connection_id}` [Solo ADMIN]
- Permite actualizar `name`, `is_global`, `adapter_type` (solo Seafile) y/o `credentials` sin devolver secretos. Cambiar el adaptador o credenciales deja la conexión inactiva hasta volver a probarla.

### `POST /api/v1/connections/{connection_id}/deactivate` [Solo ADMIN]
- Desactiva la conexión y bloquea nuevas navegaciones y transferencias.

### `POST /api/v1/connections/{connection_id}/users/{user_id}` [Solo ADMIN]
- Otorga acceso privado a una conexión.

### `DELETE /api/v1/connections/{connection_id}/users/{user_id}` [Solo ADMIN]
- Revoca ese acceso (respuesta `204 No Content`).

---

## 5. Explorador (`/connections/{id}/items`)

### `GET /api/v1/connections/{connection_id}/items`
- **Query Params:** `path=/`, `cursor=`, `page_size=50`, `sort=name`, `direction=asc`, `search=`
- **Response `200 OK`:**
```json
{
  "items": [
    {
      "name": "Backups",
      "path": "/Backups",
      "is_dir": true,
      "size_bytes": null,
      "modified_at": "2026-10-01T12:00:00Z",
      "provider_type": "MEGA"
    },
    {
      "name": "database.sql.gz",
      "path": "/database.sql.gz",
      "is_dir": false,
      "size_bytes": 104857600,
      "modified_at": "2026-10-09T08:30:00Z",
      "provider_type": "MEGA"
    }
  ],
  "next_cursor": null,
  "total_count": 2
}
```

---

## 6. Transferencias (`/transfers`)

### `POST /api/v1/transfers`
- Acepta hasta 100 rutas de origen y responde `201 Created` al registrar y agendar el trabajo. La existencia de cada ruta se valida en el worker para que una consulta lenta del proveedor no bloquee la solicitud HTTP.
- El worker aplica `TRANSFER_MAX_FILES` (predeterminado 5000), el límite opcional `TRANSFER_MAX_FILE_BYTES` (0 = sin límite fijo) y la reserva de staging antes de escribir en destino. Si el preflight falla, la transferencia pasa a `FAILED`; el usuario puede ver el mensaje seguro en `/api/v1/transfers/{transfer_id}/events`.
- **Request Body:**
```json
{
  "source_connection_id": "e267fe79-086e-4519-a749-5be01e034efe",
  "destination_connection_id": "f0596305-8513-4174-918f-76cda3dc5869",
  "source_paths": [
    "/Backups/database.sql.gz"
  ],
  "destination_path": "/Migraciones/Octubre",
  "operation": "COPY",
  "conflict_policy": "OVERWRITE_IF_DIFFERENT",
  "send_email_notification": true
}
```
- **Response `201 Created`:**
```json
{
  "id": "8e84620d-7a44-4ea4-a526-dab19de6b81c",
  "public_id": "TRF-2026-000001",
  "status": "QUEUED",
  "created_at": "2026-10-10T10:15:00Z"
}
```

### `GET /api/v1/transfers/{transfer_id}/stream`
- **Response `200 OK`:** `Content-Type: text/event-stream` emitiendo telemetría en tiempo real.

### `GET /api/v1/transfers`
- Devuelve historial paginado con `offset` y `page_size`; ADMIN ve todas las transferencias y USER solo las propias.

### `GET /api/v1/transfers/{transfer_id}`
- Devuelve el estado y métricas actuales de la transferencia visible para el usuario.

### `GET /api/v1/transfers/{transfer_id}/events`
- Devuelve eventos auditables con categorías de error y detalles técnicos sanitizados; solo el solicitante o ADMIN puede consultarlos.

### `POST /api/v1/transfers/{transfer_id}/cancel`
- **Response `200 OK`:** Envía orden de cancelación inmediata al worker.

## 7. Auditoría (`/audit-events`) [Solo ADMIN]

### `GET /api/v1/audit-events?limit=100`
- Devuelve los eventos de auditoría más recientes, con secretos sanitizados.
