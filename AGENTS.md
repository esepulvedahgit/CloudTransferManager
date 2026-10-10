# Cloud Transfer Manager — Guía Operativa para Agentes (Codex)

Instrucciones directas, invariantes y tabla de ruteo para el desarrollo autónomo mediante **OpenAI Codex** y Hermes Agent.

---

## 1. Misión y Enfoque de Implementación
Construir una aplicación web privada para explorar y transferir archivos entre servicios de almacenamiento en la nube (MVP: MEGA y Google Drive) utilizando Rclone como motor interno aislado.

### Estrategia de Desarrollo (Obligatoria):
1. **Vertical Slice Primero:** Antes de conectar APIs reales de proveedores, implementar y validar de extremo a extremo el flujo completo con adaptadores simulados (`MockStorageProvider`):
   `Login -> Conexión Mock -> Navegación Mock -> Transferencia Mock -> Progreso SSE -> Historial`.
2. **Reemplazo Incremental:** Una vez validado el vertical slice y aprobada la suite de tests, reemplazar los mocks por `RcloneMegaProvider` y `RcloneGoogleDriveProvider`.
3. **Red-Green-Refactor:** En cada fase atómica (2 a 5 minutos), escribir pruebas que fallen, implementar la lógica mínima para pasar y refactorizar. No avanzar de fase si hay tests fallidos.

---

## 2. Invariantes del Repositorio (Reglas Inviolables)

1. **Seguridad Rclone:**
   - **NUNCA** invocar Rclone mediante `shell=True`. Invocar siempre pasando argumentos como lista de strings (`subprocess.Popen([...])` o wrapper async).
   - **NUNCA** concatenar parámetros o rutas del usuario directamente en comandos de shell.
   - **NUNCA** exponer la API RC de Rclone a la red exterior ni a usuarios.
   - **Lista blanca estricta de comandos:** Solo se permiten `lsjson`, `copy`, `about`, `size`.
   - **Prohibido terminantemente:** Comandos destructivos como `delete`, `purge`, `sync`, `move`, `moveto`, `rmdir`, `dedupe`, `serve`, `mount`, `config dump`.
   - Generar un archivo de configuración temporal por trabajo con permisos `0600` y eliminarlo inmediatamente al finalizar (éxito, fallo o cancelación).
2. **Cero Operaciones Destructivas en MVP:**
   - Solo operación `COPY`. Nunca eliminar ni alterar archivos en el almacenamiento de origen.
3. **Cero Secretos en Repositorio y Logs:**
   - No incluir credenciales por defecto, tokens ni llaves maestras en el código fuente, commits o logs estructurados.
   - Contraseñas de usuario: Almacenar obligatoriamente con **Argon2id**.
   - Credenciales de proveedores: Cifrar en PostgreSQL usando **AES-256-GCM / Envelope Encryption** con Master Key montada externamente vía Docker Secret.
   - El frontend NUNCA debe recibir las credenciales guardadas de un proveedor.
4. **Identificadores y Modelos:**
   - Identificadores internos en base de datos: Siempre `UUID` (v4).
   - Identificadores públicos de transferencias: Código legible `TRF-YYYY-NNNNNN`.
5. **Red Privada y Servicios Internos:**
   - PostgreSQL, Redis, Workers y Rclone no deben publicar puertos fuera de la red interna de Docker. Solo el Reverse Proxy expone la aplicación.

---

## 3. Stack Tecnológico

| Capa | Tecnologías |
|---|---|
| **Backend** | Python 3.12+, FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic v2 |
| **Workers & Colas** | Celery / Celery Beat, Redis (Broker + Pub/Sub para SSE) |
| **Base de Datos** | PostgreSQL 16+ con extensión pgcrypto/uuid |
| **Motor de Transferencia** | Rclone (binario CLI invocado como subproceso controlado) |
| **Frontend** | React 18+, TypeScript, Vite, Material UI (MUI v5/v6), TanStack Query, React Router, React Hook Form, Zod |
| **Telemetría** | Server-Sent Events (SSE) para progreso unidireccional |
| **Infraestructura** | Docker Compose, Nginx (Reverse Proxy), Docker Secrets |

---

## 4. Comandos de Construcción y Verificación

### Backend (`/backend`):
```bash
# Entorno virtual y dependencias
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Ejecutar suite de pruebas (TDD)
pytest -v --tb=short

# Linter y chequeo de tipos
ruff check .
mypy app

# Migraciones de base de datos
alembic upgrade head

# CLI de creación de administrador inicial
python -m app.cli create-admin --username admin --email admin@intranet.local
```

### Frontend (`/frontend`):
```bash
# Dependencias
npm ci

# Tests unitarios y de componentes
npm test

# Linter y build de producción
npm run lint
npm run build
```

### Despliegue Local Docker:
```bash
docker compose up -d --build
docker compose ps
docker compose logs -f backend worker
```

---

## 5. Tabla de Ruteo a Especificaciones (`docs/`)

Antes de implementar o modificar cualquier módulo, el agente **DEBE** consultar su documento de especificación formal:

| Módulo / Funcionalidad | Especificación Detallada | Alcance Técnico |
|---|---|---|
| **Visión & Alcance MVP** | `docs/specs/00-mvp-overview-spec.md` | Restricciones, 30 criterios de aceptación, fuera de alcance |
| **Autenticación & Usuarios** | `docs/specs/01-auth-users-spec.md` | Argon2id, roles (ADMIN/USER), sesiones HttpOnly, CLI `create-admin` |
| **Proveedores de Storage** | `docs/specs/02-storage-providers-spec.md` | Interfaz `StorageProvider`, MEGA, Google Drive OAuth, cifrado AES-GCM |
| **Explorador de Archivos** | `docs/specs/03-file-explorer-spec.md` | Dual-pane desktop, wizard móvil, paginación, filtros, navegación |
| **Motor de Transferencias** | `docs/specs/04-transfers-engine-spec.md` | Celery worker, Rclone subprocess seguro, máquina de estados, retries |
| **Telemetría en Vivo (SSE)** | `docs/specs/05-realtime-telemetry-spec.md` | Endpoint SSE, Redis Pub/Sub, métricas (speed, ETA, bytes), reconexión |
| **Auditoría & Correo** | `docs/specs/06-audit-notifications-spec.md` | Logs inmutables `AuditEvent`, `TransferEvent`, templates SMTP background |
| **Infraestructura Docker** | `docs/specs/07-infrastructure-docker-spec.md` | `compose.yaml`, secrets, volúmenes, healthchecks `/health/live` y `/ready` |
| **Contratos de API REST** | `docs/api/openapi-contracts.md` | Endpoints, verbos HTTP, schemas JSON de petición y respuesta |

### Decisiones de Arquitectura (ADRs):
- **`docs/adr/ADR-001-rclone-subprocess-security.md`**: Por qué se utiliza subproceso CLI aislado y no la API RC de Rclone.
- **`docs/adr/ADR-002-envelope-credential-encryption.md`**: Esquema de cifrado en reposo para credenciales de almacenamiento.
- **`docs/adr/ADR-003-server-sent-events-telemetry.md`**: Elección de Server-Sent Events sobre WebSockets.

---

## 6. Fases de Ejecución para Codex

- [ ] **Fase 1: Base del Proyecto & Andamiaje** (Monorepo, docker-compose base, DB, Redis, linting, tests).
- [ ] **Fase 2: Vertical Slice con Mocks** (Auth básico, MockStorageProvider, transferencia mock, SSE mock).
- [ ] **Fase 3: Autenticación, Roles y CLI** (Argon2id, sesiones seguras, CRUD usuarios, CLI `create-admin`).
- [ ] **Fase 4: Proveedores & Cifrado Seguro** (AES-256-GCM, adaptador MEGA, adaptador Google Drive OAuth, test conexión).
- [ ] **Fase 5: Explorador de Archivos** (Backend `/items`, UI dual-pane/móvil, paginación, selección múltiple).
- [ ] **Fase 6: Motor de Transferencias Real** (Rclone CLI aislado, Celery worker, máquina de estados, cancelación, retries).
- [ ] **Fase 7: Telemetría SSE & Notificaciones** (Redis Pub/Sub, métricas en vivo, plantillas email SMTP en background).
- [ ] **Fase 8: Auditoría, Hardening & Criterios de Aceptación** (Audit logs inmutables, rate limits, headers, verificación E2E).
