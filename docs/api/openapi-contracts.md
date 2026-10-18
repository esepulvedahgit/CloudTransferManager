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
  "role": "ADMIN",
  "must_change_password": true
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
- **Response `200 OK`:** Devuelve `id`, `username`, `email`, `role` y `must_change_password` del usuario autenticado.

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

## 4. Conexiones (`/connections`) [Creación y administración solo ADMIN]

`GET /api/v1/connections` devuelve únicamente las conexiones globales o asignadas al usuario. Los campos de credenciales nunca forman parte de las respuestas.
Las conexiones MEGA, Google Drive, Seafile, SMB y Nextcloud nuevas quedan `INACTIVE` hasta superar la prueba; las simuladas quedan activas. Un administrador puede ejecutar la prueba desde el explorador o mediante el endpoint siguiente. Nextcloud aparece en el selector de proveedores y cada administrador define la URL del servidor en la conexión.

### `GET /api/v1/system/capabilities`
- Requiere sesión autenticada. Devuelve `enabled_providers`, la lista de proveedores disponibles en este despliegue, incluido Nextcloud.

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

- **Request Body (SMB/NAS):**
```json
{
  "name": "NAS de proyectos",
  "provider_type": "SMB",
  "is_global": false,
  "credentials": {
    "host": "nas.example.intranet",
    "port": 445,
    "share": "proyectos",
    "base_path": "",
    "domain": "INTRANET",
    "username": "operador",
    "password": "secreto"
  }
}
```
El host debe coincidir exactamente con una entrada de `SMB_ALLOWED_TARGETS`. SMB se usa como origen; el operador debe preparar la ruta y el firewall hacia el NAS.

- **Request Body (Nextcloud):**
```json
{
  "name": "Nextcloud de equipo",
  "provider_type": "NEXTCLOUD",
  "is_global": true,
  "credentials": {
    "url": "https://cloud.example.com",
    "username": "alice",
    "password": "contraseña-de-aplicación"
  }
}
```
- `url` es la URL base HTTPS y puede incluir un prefijo de instalación. El backend genera `/remote.php/dav/files/{usuario}/`. Las credenciales nunca se incluyen en la respuesta. La conexión se guarda inactiva hasta superar el test de raíz.

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
- **Query Params:** `path=/`, `parent_object_id=`, `cursor=`, `page_size=50`, `sort=name`, `direction=asc`, `search=`. Envía `parent_object_id` cuando el proveedor expone IDs y se explora una carpeta homónima.
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
      "provider_type": "MEGA",
      "object_id": "H:AbCd1234",
      "parent_object_id": "H:Root1234"
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
- Requiere `Idempotency-Key` con un UUID nuevo por intención de copia. Repetir la clave con el mismo usuario y los mismos datos devuelve la transferencia existente; usarla con datos distintos responde `409 Conflict`.
- Acepta hasta 100 rutas de origen y responde `201 Created` al registrar y agendar el trabajo. La existencia de cada ruta se valida en el worker para que una consulta lenta del proveedor no bloquee la solicitud HTTP. `source_item_ids` y `source_parent_ids` son listas opcionales, alineadas con `source_paths`, para conservar identidad en proveedores con nombres duplicados.
- En solicitudes nuevas, `conflict_policy` es siempre `KEEP_BOTH` (también es el valor predeterminado si se omite). `SKIP` y `OVERWRITE_IF_DIFFERENT` explícitos se rechazan con `422`. Los trabajos históricos mantienen su política almacenada.
- SMB solo se admite como origen de migración; el API rechaza una conexión SMB como destino con `422`.
- `scheduled_at` opcional acepta una fecha futura con offset; el servidor la normaliza a UTC. `schedule_timezone` debe ser una zona IANA y conserva la zona mostrada en la interfaz. Si no se envía `scheduled_at`, el trabajo queda en la cola normal.
- `execution_mode` opcional admite `AUTO` (predeterminado) o `EXCLUSIVE`. `depends_on_transfer_id` es opcional y solo libera el trabajo cuando el padre finaliza correctamente, incluso con advertencias.
- `source_file_error_policy` se fija en `STOP`. La opción `SKIP_CONFIRMED_RESTRICTED` se rechaza con `422` hasta validar una firma de restricción individual para la versión instalada de MEGAcmd; los errores sin esa evidencia nunca se omiten automáticamente.
- Una solicitud con fecha futura crea `evaluation` en estado `PENDING` dentro de la misma transacción. La evaluación recorre metadatos del origen antes de la hora programada, no descarga ni copia datos y no reserva capacidad de ejecución.
- Si el conteo supera el límite configurado, la evaluación queda `BLOCKED` y la transferencia permanece en Programaciones sin reservar una copia. Las transferencias inmediatas conservan el preflight del worker; si falla, pasan a `FAILED`. En ambos casos se conserva la validación final al comenzar la copia.
- El worker de copia aplica `TRANSFER_MAX_FILES` (predeterminado 5000), `SMB_TRANSFER_MAX_FILES` para orígenes SMB (predeterminado 100000), el límite opcional `TRANSFER_MAX_FILE_BYTES` (0 = sin límite fijo) y la reserva de staging antes de escribir en destino. Los errores se consultan en `/api/v1/transfers/{transfer_id}/events`.
- **Request Body:**
```json
{
  "source_connection_id": "e267fe79-086e-4519-a749-5be01e034efe",
  "destination_connection_id": "f0596305-8513-4174-918f-76cda3dc5869",
  "source_paths": [
    "/Backups/database.sql.gz"
  ],
  "source_item_ids": ["H:AbCd1234"],
  "source_parent_ids": ["H:Root1234"],
  "destination_path": "/Migraciones/Octubre",
  "operation": "COPY",
  "conflict_policy": "KEEP_BOTH",
  "send_email_notification": true,
  "scheduled_at": "2026-11-20T18:45:00-03:00",
  "schedule_timezone": "America/Santiago",
  "execution_mode": "AUTO",
  "source_file_error_policy": "STOP",
  "depends_on_transfer_id": null
}
```
- **Response `201 Created`:**
```json
{
  "id": "8e84620d-7a44-4ea4-a526-dab19de6b81c",
  "public_id": "TRF-2026-000001",
  "status": "SCHEDULED",
  "scheduled_at": "2026-11-20T21:45:00Z",
  "schedule_timezone": "America/Santiago",
  "execution_mode": "AUTO",
  "wait_reason": "WAITING_UNTIL",
  "evaluation": {
    "status": "PENDING",
    "generation": 1,
    "sequence": 0,
    "files_found": null,
    "count_complete": false,
    "max_files": 5000,
    "started_at": null,
    "updated_at": null,
    "completed_at": null,
    "safe_error_code": null,
    "safe_message": null
  },
  "created_at": "2026-10-10T10:15:00Z"
}
```

### `GET /api/v1/transfers/capabilities`
- Informa si hay programación, recurrencia (por ahora `false`), modos de ejecución, capacidad global y límites efectivos de archivos: `max_files_by_source_provider.default` para orígenes cloud y `max_files_by_source_provider.SMB` para orígenes SMB.

### `PATCH /api/v1/transfers/{transfer_id}`
- Actualiza `scheduled_at`, `schedule_timezone`, `execution_mode` y `depends_on_transfer_id` mientras el trabajo siga `SCHEDULED` o `QUEUED` y todavía no tenga reserva de despacho.
- Devuelve `409` si la transferencia ya fue admitida. Las fechas deben ser futuras; enviar `scheduled_at: null` la deja lista para ejecutarse al liberarse la cola.
- Al quitar la fecha se conserva cualquier evaluación creada. Cambiar solo hora o modo conserva la aprobación; cambiar la dependencia inicia una generación nueva y vuelve a contar.
- `wait_reason` también puede ser `WAITING_EVALUATION` o `BLOCKED_EVALUATION`, además de `WAITING_UNTIL`, `WAITING_PARENT`, `BLOCKED_PARENT_FAILED`, `WAITING_CAPACITY`, `WAITING_EXCLUSIVE`, `WAITING_CONNECTION`, `WAITING_STAGING`, `WAITING_CONNECTION_ACTIVE`, `WAITING_CONNECTION_ACCESS`, `WAITING_USER_ACTIVE` o `BLOCKED_CONNECTION_CHANGED`.
- `can_edit_schedule` indica si el trabajo aún admite reprogramación.

### `POST /api/v1/transfers/{transfer_id}/evaluation`
- Solicita una evaluación nueva para una transferencia pendiente. Requiere sesión del propietario o ADMIN y token CSRF.
- Devuelve `202 Accepted` con el objeto público `evaluation`. Si ya está `PENDING`, `EVALUATING` o `WAITING_PARENT`, devuelve la generación vigente sin duplicarla.
- Responde `409 Conflict` para una transferencia admitida, en ejecución o terminal, o si no tiene una evaluación anticipada.
- El objeto público incluye `status`, `generation`, `sequence`, `files_found`, `count_complete`, `max_files`, fechas y mensaje/código seguro; nunca expone claim tokens, versiones de credenciales ni credenciales.

### `GET /api/v1/transfers/{transfer_id}/stream`
- **Response `200 OK`:** `Content-Type: text/event-stream` emitiendo telemetría en tiempo real.
- Cada snapshot y evento incluye el mismo objeto público `evaluation`; el cliente ordena sus cambios mediante `generation` y `sequence`, independientes del progreso de copia.

### `GET /api/v1/transfers`
- Devuelve historial paginado con `offset` y `page_size`; ADMIN ve todas las transferencias y USER solo las propias.
- `pending_only=true` limita la lista a estados `SCHEDULED` y `QUEUED`, para recorrer la cola completa por páginas.
- `background_only=true` limita la lista a tareas administradas por **Programaciones** que sigan en estados no terminales: `SCHEDULED`, `QUEUED`, `VALIDATING`, `RUNNING`, `RETRYING` o `CANCELLING`. Una tarea pertenece a Programaciones cuando tiene `scheduled_at`, `wait_reason`, modo `EXCLUSIVE` o una transferencia padre (`depends_on_transfer_id`). La respuesta conserva `total_count` después de aplicar el filtro de propietario.
- `exclude_background=true` omite únicamente esas tareas no terminales de Programaciones. Las transferencias terminales, incluidas las programadas, siguen disponibles en la lista general.
- Las respuestas de creación, detalle, listado y SSE incluyen `evaluation`. Si no se requiere evaluación anticipada, el estado es `NOT_REQUIRED`. Un conteo nulo significa que la cantidad todavía se desconoce; solo `count_complete=true` confirma el total.
- Cada elemento incluye `manifest_summary`: `copied` son archivos copiados y verificados, `failed` son archivos con error, `skipped` cuenta archivos omitidos, `omitted` identifica el subconjunto con omisión explícita y `pending` son archivos aún no resueltos. `omitted_bytes` suma el tamaño conocido de ese subconjunto y no se agrega a bytes transferidos. Los contadores de archivos excluyen entradas de directorio. En SMB, `files_completed` cuenta elementos resueltos y no equivale a `copied`.

### `GET /api/v1/transfers/{transfer_id}`
- Devuelve el estado y métricas actuales de la transferencia visible para el usuario.

### `GET /api/v1/transfers/{transfer_id}/events`
- Devuelve eventos auditables con categorías de error y detalles técnicos sanitizados; solo el solicitante o ADMIN puede consultarlos.

### `GET /api/v1/transfers/{transfer_id}/items`
- Devuelve una página del manifiesto común (entradas de directorio y estados `PENDING`, `COPIED`, `SKIPPED` o `FAILED`). Las filas incluyen identidad de origen, `requested_destination_path`, `destination_path` asignada y `rename_reason` cuando aplica. `can_skip` y `skip_unavailable_reason` describen la elegibilidad actual de omisión manual; `can_retry_omission` marca solo omisiones explícitas reintentables.
- Acepta `after_id` como cursor y `limit` de 1 a 200; la respuesta incluye `next_cursor` y conteos agregados, incluido `omitted` y `omitted_bytes`.

### `POST /api/v1/transfers/{transfer_id}/items/{item_id}/skip-and-resume`
- Requiere CSRF y cuerpo `{ "expected_attempt": 2, "request_id": "<UUID>", "reason": "USER_ACCEPTED_SOURCE_FAILURE" }`. Omite un archivo elegible y reanuda la transferencia de manera atómica e idempotente. La respuesta es `202 Accepted`; `409` indica estado, intento o evidencia obsoletos; `404` oculta transferencias o filas fuera del alcance del usuario.
- La confirmación significa que ese archivo no será copiado. `source_file_error_policy` puede omitirse o ser `STOP`; pedir `SKIP_CONFIRMED_RESTRICTED` responde `422` mientras la clasificación automática siga deshabilitada.

### `POST /api/v1/transfers/{transfer_id}/retry-omitted`
- Requiere CSRF. Cuerpo: `{ "expected_attempt": 3, "request_id": "<UUID>", "item_ids": ["<UUID>"] }`, con entre 1 y 100 UUID únicos. Reintenta únicamente omisiones explícitas seleccionadas de una transferencia terminal, conserva la auditoría previa y responde `202 Accepted`.
- `409` indica estado/intento obsoleto, reserva activa o fila no reintentable. La misma clave de solicitud y el mismo contenido son idempotentes.

### `GET /api/v1/transfers/{transfer_id}/omissions.csv`
- Descarga CSV paginado de las omisiones explícitas de una transferencia terminal. Solo el propietario o ADMIN puede acceder. Se escapan celdas y se neutralizan prefijos de fórmula; el endpoint no está disponible antes del estado terminal (`409`).

### `POST /api/v1/transfers/{transfer_id}/resume`
- Disponible para transferencias nuevas `KEEP_BOTH` y para trabajos SMB históricos en estado `FAILED` o `CANCELLED`, cuando el manifiesto conserva archivos pendientes/fallidos y ambas conexiones siguen activas.
- Acepta sin cuerpo las llamadas existentes; opcionalmente `{ "source_file_error_policy": "STOP" }`. Rechaza con `422` `SKIP_CONFIRMED_RESTRICTED` hasta verificar una firma individual de la versión instalada de MEGAcmd.
- Requiere las mismas versiones de credenciales capturadas por el trabajo y, para SMB, la misma raíz del NAS. Reutiliza los nombres ya asignados y responde `202 Accepted`. Las escrituras ambiguas se verifican por SHA-256 o se detienen para evitar duplicar.

### `POST /api/v1/transfers/{transfer_id}/accept-parent-omissions`
- Para una transferencia hija bloqueada por `BLOCKED_PARENT_OMISSIONS`, requiere CSRF y cuerpo `{ "expected_parent_attempt": 3, "request_id": "<UUID>" }`. Acepta explícitamente las omisiones del intento indicado del padre terminal y responde `202 Accepted`; no despacha el trabajo desde el navegador.
- Una repetición del padre invalida la aceptación previa. `409` indica que el hijo, padre o intento ya cambió; evaluación, dispatcher y worker comprueban de nuevo el intento aceptado.

### `POST /api/v1/transfers/{transfer_id}/cancel`
- Para una transferencia pendiente sin reserva, la cancela de inmediato. Si ya fue admitida, envía la orden al worker y pasa a `CANCELLING`.

## 7. Auditoría (`/audit-events`) [Solo ADMIN]

### `GET /api/v1/audit-events?limit=100`
- Devuelve los eventos de auditoría más recientes, con secretos sanitizados.

### `GET /api/v1/notifications/outbox?limit=100` [Solo ADMIN]
- Muestra estado, intentos, próxima ejecución y código de error seguro de las notificaciones de transferencia.
- No devuelve correo del destinatario ni la instantánea interna del mensaje.
- SMTP se ejecuta en una cola y worker separados; los errores vuelven a PostgreSQL con reintentos crecientes, hasta cinco intentos. El despacho es al menos una vez: una caída justo después de que el servidor SMTP acepte el correo pero antes de guardar `SENT` puede producir un duplicado.

## 8. Salud del servicio

### `GET /health/live`
- `200 OK` cuando el proceso HTTP responde.

### `GET /health/ready`
- `200 OK` cuando el almacenamiento, Redis y los componentes requeridos para el modo de despliegue están disponibles.
- `503 Service Unavailable` cuando falta una dependencia o un worker Celery requerido. En producción comprueba `transfers` y `control`; el worker de correo se supervisa por separado.

### `GET /health/notifications`
- `200 OK` cuando hay un consumidor activo de la cola `notifications`; en entornos no productivos devuelve `not_required`.
- `503 Service Unavailable` cuando la cola de correo no tiene un consumidor activo.

### Resumen de manifiesto (extensión compatible, 2026-10-16)

Los listados, detalle y eventos de progreso de transferencias incluyen `manifest_summary`: `{copied, failed, skipped, pending, renamed, omitted, omitted_bytes}` (enteros no negativos). El primer snapshot SSE lo incluye también para trabajos terminales. `renamed` cuenta nombres asignados por conflictos/límites, incluidos directorios; no equivale a `warning_count`. `omitted` es el subconjunto explícito de `skipped`; los conflictos históricos no se pueden reintentar mediante el endpoint de omisiones. `files_completed` continúa contando elementos resueltos para compatibilidad. Los consumidores muestran copiados desde `manifest_summary.copied` y no declaran 100% con elementos sin copiar.
