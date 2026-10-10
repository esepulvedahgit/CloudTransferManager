# Contratos de API REST (Cloud Transfer Manager)

Base URL: `/api/v1`

---

## 1. Autenticación (`/auth`)

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

### `POST /api/v1/users/{user_id}/deactivate`
- **Response `200 OK`:** Revoca sesiones activas e inhabilita usuario.

---

## 3. Conexiones (`/connections`)

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
    "refresh_token": "..."
  }
}
```

### `POST /api/v1/connections/{connection_id}/test`
- **Response `200 OK`:**
```json
{
  "success": true,
  "message": "Conexión verificada exitosamente",
  "quota_total_bytes": 21474836480,
  "quota_used_bytes": 5368709120
}
```

---

## 4. Explorador (`/connections/{id}/items`)

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

## 5. Transferencias (`/transfers`)

### `POST /api/v1/transfers`
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

### `POST /api/v1/transfers/{transfer_id}/cancel`
- **Response `200 OK`:** Envía orden de cancelación inmediata al worker.
