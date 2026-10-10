# Especificación 01: Autenticación, Usuarios y Permisos

## 1. Contexto y Objetivos
Proveer un subsistema de autenticación local robusto, seguro y libre de tokens expuestos en almacenamiento del navegador. El sistema maneja dos roles principales (`ADMIN` y `USER`), gestiona sesiones seguras y permite aprovisionar el primer superadministrador mediante CLI.

---

## 2. Modelo de Datos de Usuario

```sql
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(64) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(16) NOT NULL CHECK (role IN ('ADMIN', 'USER')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
    failed_login_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMP WITH TIME ZONE NULL,
    last_login_at TIMESTAMP WITH TIME ZONE NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    created_by UUID NULL REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX idx_users_username ON users(username);
CREATE INDEX idx_users_email ON users(email);
```

### Reglas de Seguridad en Credenciales:
- **Hash de Contraseñas:** Obligatoriamente **Argon2id** (usando `pwdlib` o `passlib` con configuración estándar OWASP: memoria >= 64MB, iteraciones >= 3, paralelismo >= 4).
- **Nunca texto plano:** Prohibido almacenar contraseñas en texto claro o con algoritmos obsoletos (MD5, SHA1, SHA256 simple).

---

## 3. Manejo de Sesiones Web
- **Tipo de Sesión:** Cookies de sesión firmadas/cifradas (`HttpOnly`, `SameSite=Lax`, `Secure` configurable por `SESSION_COOKIE_SECURE`).
- **No LocalStorage:** No utilizar JWT en `localStorage` o `sessionStorage` para prevenir ataques XSS.
- **CSRF:** Tokens de protección anti-CSRF (Double Submit Cookie o cabecera `X-CSRF-Token`).
- **Caducidad:** Expiración configurable (`SESSION_MAX_AGE_SECONDS=28800` / 8 horas).
- **Rotación de Sesión:** Regenerar el identificador de sesión inmediatamente tras un inicio de sesión exitoso (prevención de Session Fixation).
- **Desactivación Inmediata:** Desactivar un usuario en base de datos (`is_active = FALSE`) revoca inmediatamente todas sus sesiones activas (comprobado vía middleware o Redis session store).

---

## 4. Política contra Fuerza Bruta y Lockout
- Cada intento fallido incrementa `failed_login_attempts`.
- Si `failed_login_attempts >= 5`: Bloquear la cuenta durante 15 minutos (`locked_until = NOW() + INTERVAL '15 minutes'`).
- Los mensajes de error de login **nunca deben revelar** si el usuario o correo existe:
  > Mensaje estándar: *"No fue posible iniciar sesión. Revisa tus datos e inténtalo nuevamente."*

---

## 5. Comando CLI para Administrador Inicial
No debe existir ningún usuario ni contraseña predeterminada en el código fuente ni en migraciones.
Al inicializar la aplicación, el administrador inicial se crea mediante:

```bash
docker compose exec backend python -m app.cli create-admin --username admin --email admin@intranet.local --password "ContraseñaSegura123!"
```
*(Si no se pasa `--password`, el CLI solicitará la contraseña enmascarada interactivamente).*

---

## 6. Matriz de Permisos (RBAC)

| Acción / Recurso | Administrador (`ADMIN`) | Usuario (`USER`) |
|---|---|---|
| Iniciar / Cerrar sesión | Sí | Sí |
| Cambiar contraseña propia | Sí | Sí |
| Crear / Editar / Desactivar usuarios | Sí | **No** |
| Restablecer contraseñas de otros | Sí | **No** |
| Configurar / Probar conexiones globales | Sí | **No** (solo lectura si se le asigna) |
| Navegar por conexiones autorizadas | Sí (Todas) | Sí (Solo asignadas) |
| Crear transferencias | Sí | Sí |
| Ver historial propio | Sí | Sí |
| Ver historial de otros usuarios | Sí | **No** |
| Cancelar transferencias de otros | Sí | **No** |
| Configurar SMTP y Ajustes Globales | Sí | **No** |
| Ver Logs de Auditoría del Sistema | Sí | **No** |

---

## 7. Estrategia de Pruebas (TDD)
- **Unitarias:**
  - `test_hash_password_argon2id`: Verificación de cálculo y validación de hash.
  - `test_rate_limit_lockout`: Simulación de 5 fallos consecutivos y verificación de `locked_until`.
  - `test_session_cookie_headers`: Verificación de flags `HttpOnly`, `SameSite=Lax`.
- **Integración:**
  - `test_cli_create_admin`: Ejecución del CLI y verificación de creación en BD.
  - `test_rbac_user_cannot_create_user`: Verificación de retorno `403 Forbidden`.
  - `test_deactivated_user_session_rejected`: Middleware bloquea peticiones de usuario inactivo.
