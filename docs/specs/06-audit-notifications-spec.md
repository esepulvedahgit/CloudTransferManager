# Especificación 06: Auditoría, Eventos y Notificaciones por Correo

## 1. Contexto y Objetivos
Garantizar la trazabilidad completa, la observabilidad forense y la comunicación automatizada con los usuarios y administradores sobre las operaciones del sistema, manteniendo una política estricta de sanitización de secretos.

---

## 2. Modelo de Auditoría (`audit_events`)

Registra toda acción sensible ejecutada en la plataforma:

```sql
CREATE TABLE audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    action VARCHAR(64) NOT NULL,
    resource_type VARCHAR(64) NOT NULL,
    resource_id VARCHAR(64) NULL,
    result VARCHAR(16) NOT NULL CHECK (result IN ('SUCCESS', 'FAILURE')),
    ip_address VARCHAR(45) NULL,
    user_agent VARCHAR(255) NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_events_action ON audit_events(action);
CREATE INDEX idx_audit_events_created_at ON audit_events(created_at);
```

### Eventos Auditados Obligatoriamente:
- Inicio de sesión (exitoso y fallido) y cierre de sesión.
- Cambio y restablecimiento de contraseña.
- Creación, modificación y desactivación de usuarios.
- Creación, prueba, edición y desactivación de conexiones.
- Creación, cancelación y reintento de transferencias.
- Modificación de ajustes de sistema o configuración SMTP.
- Accesos no autorizados (intentos 403 / 401).

---

## 3. Modelo de Eventos de Transferencia (`transfer_events`)

```sql
CREATE TABLE transfer_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transfer_job_id UUID NOT NULL REFERENCES transfer_jobs(id) ON DELETE CASCADE,
    level VARCHAR(16) NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')),
    event_type VARCHAR(64) NOT NULL,
    message TEXT NOT NULL,
    technical_details JSONB NULL,
    provider_error_code VARCHAR(64) NULL,
    item_path VARCHAR(1024) NULL,
    correlation_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
```

---

## 4. Política de Sanitización Estricta de Secretos
**Regla de Oro:** Ningún registro de log, evento de auditoría ni detalle técnico de error puede contener:
- Contraseñas o hashes.
- Tokens OAuth (Refresh Tokens, Access Tokens).
- Claves maestras de cifrado.
- Cabeceras `Authorization` o `Cookie`.
- Dumps de configuración de Rclone con contraseñas.

---

## 5. Notificaciones por Correo Electrónico (SMTP)

### Configuración de Administración
- Los administradores gestionan la configuración global desde una subpestaña SMTP del diálogo Administración.
- `GET /api/v1/settings/smtp` devuelve host, puerto, STARTTLS, remitente e indicador `credentials_configured`; nunca devuelve usuario ni contraseña. `PUT` actualiza esos valores y `POST /api/v1/settings/smtp/test` prueba el borrador sin guardarlo ni enviar un mensaje.
- Solo el rol `ADMIN` puede leer, probar o modificar la configuración. Las mutaciones requieren protección CSRF.
- Si no existe un registro guardado, la configuración y la contraseña montada como Docker Secret siguen tomando sus valores del entorno actual. Después de guardar, los workers usan el registro de PostgreSQL para los siguientes correos.
- Usuario y contraseña se cifran juntos mediante AES-256-GCM envelope con la clave maestra del sistema. Los campos de host, puerto, TLS y remitente no son secretos.
- Credenciales omitidas o vacías conservan las credenciales vigentes. La prueba autentica solo cuando existen credenciales y devuelve un resultado genérico ante fallos SMTP, TLS, autenticación o timeout.
- Cada actualización registra `SMTP_SETTINGS_UPDATED`. Sus metadatos contienen nombres de campos cambiados, nunca valores SMTP ni credenciales.

### Triggers de Envío:
- Transferencia Completada (`COMPLETED`).
- Transferencia Completada con Advertencias (`COMPLETED_WITH_WARNINGS`).
- Transferencia Fallida (`FAILED`).
- Transferencia Cancelada (`CANCELLED`).
- Error Crítico de Conexión de Proveedor (Solo a Administradores).

### Reglas de Implementación:
- El envío de correos se procesa en segundo plano como una tarea Celery separada.
- Un fallo en el servidor SMTP **NUNCA** debe marcar una transferencia exitosa como fallida.
- Los correos no contienen rutas temporales locales ni stack traces técnicos.

---

## 6. Estrategia de Pruebas (TDD)
- **Unitarias:**
  - `test_audit_logger_sanitizes_passwords`: Metadatos que contienen claves como `password` o `token` son reemplazados por `[REDACTED]`.
  - `test_email_template_rendering`: Las plantillas de correo renderizan los datos clave (ID, origen, destino, duración, tamaño).
- **Integración:**
  - `test_transfer_completion_dispatches_email_task`: Completar un trabajo agenda la tarea Celery de notificación.
