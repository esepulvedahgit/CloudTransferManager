# Cloud Transfer Manager — Guía Operativa para Agentes (Codex)

Instrucciones directas, invariantes y tabla de ruteo para el desarrollo autónomo mediante **OpenAI Codex** y Hermes Agent.

---

## 1. Misión y Enfoque de Implementación
Mantener una aplicación web privada para explorar y copiar archivos entre proveedores cloud y recursos SMB/NAS, con Rclone y MEGAcmd como motores internos controlados. El alcance inicial MEGA↔Google Drive ya se amplió con Seafile, SMB/NAS, programación de transferencias y administración SMTP. El estado verificable se resume en `docs/implementation-status.md`.

### Estrategia de Desarrollo (Obligatoria):
1. **Vertical Slice Primero (fase completada):** El flujo con adaptadores simulados (`MockStorageProvider`) permanece cubierto como base de desarrollo y pruebas:
   `Login -> Conexión Mock -> Navegación Mock -> Transferencia Mock -> Progreso SSE -> Historial`.
2. **Proveedores reales (implementados):** MEGA usa MEGAcmd; Google Drive y Seafile/Rclone usan Rclone; Seafile API es opcional por conexión; SMB/NAS se usa como origen de migraciones. No activar una conexión hasta probarla contra el servicio real de la instalación.
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
| **Base de Datos** | PostgreSQL 16 |
| **Motores de Transferencia** | Rclone y MEGAcmd como subprocesos controlados; adaptador HTTPX opcional para Seafile |
| **Frontend** | React 18–19, TypeScript 5, Vite, Material UI 6–7, TanStack Query 5, React Router 6–7, React Hook Form, Zod |
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
| **Proveedores de Storage** | `docs/specs/02-storage-providers-spec.md` | Interfaz `StorageProvider`, MEGA, Google Drive OAuth, Seafile, SMB/NAS y cifrado AES-GCM |
| **Explorador de Archivos** | `docs/specs/03-file-explorer-spec.md` | Paneles coordinados, pasos para pantallas estrechas, paginación, búsqueda y navegación |
| **Motor de Transferencias** | `docs/specs/04-transfers-engine-spec.md` | Celery worker, Rclone subprocess seguro, máquina de estados, retries |
| **Telemetría en Vivo (SSE)** | `docs/specs/05-realtime-telemetry-spec.md` | Endpoint SSE, Redis Pub/Sub, métricas (speed, ETA, bytes), reconexión |
| **Auditoría & Correo** | `docs/specs/06-audit-notifications-spec.md` | Eventos append-only desde la aplicación, sanitización y notificaciones SMTP en background; revisar enforcement de inmutabilidad en BD |
| **Infraestructura Docker** | `docs/specs/07-infrastructure-docker-spec.md` | `compose.yaml`, secrets, volúmenes, healthchecks `/health/live` y `/ready` |
| **Contratos de API REST** | `docs/api/openapi-contracts.md` | Endpoints, verbos HTTP, schemas JSON de petición y respuesta |
| **Estado de implementación** | `docs/implementation-status.md` | Capacidades presentes, extensiones, fases y validaciones aún requeridas |
| **SMB/NAS** | `docs/integrations/smb-nas-setup.md` | Allowlist de hosts, red, exploración y migración por manifiesto |
| **Programaciones** | `docs/integrations/transfer-scheduling.md` | Ejecución única, modos, dependencias y cola de segundo plano |
| **Seafile** | `docs/integrations/seafile-compatibility.md` | Adaptadores disponibles y límites de compatibilidad |
| **Nextcloud** | `docs/integrations/nextcloud-compatibility.md` | WebDAV/Rclone, allowlist HTTPS, límites operativos y validación real |

### Decisiones de Arquitectura (ADRs):
- **`docs/adr/ADR-001-rclone-subprocess-security.md`**: Por qué se utiliza subproceso CLI aislado y no la API RC de Rclone.
- **`docs/adr/ADR-002-envelope-credential-encryption.md`**: Esquema de cifrado en reposo para credenciales de almacenamiento.
- **`docs/adr/ADR-003-server-sent-events-telemetry.md`**: Elección de Server-Sent Events sobre WebSockets.

---

## 6. Fases de Ejecución para Codex

- [x] **Fase 1: Base del Proyecto & Andamiaje** (monorepo, Compose, DB, Redis, lint y pruebas).
- [x] **Fase 2: Vertical Slice con Mocks** (auth, MockStorageProvider, transferencia mock, SSE e historial).
- [x] **Fase 3: Autenticación, Roles y CLI** (Argon2id, sesiones seguras, usuarios, CLI `create-admin`).
- [x] **Fase 4: Proveedores & Cifrado Seguro** (AES-256-GCM, MEGAcmd, Google Drive OAuth, Seafile y SMB).
- [x] **Fase 5: Explorador de Archivos** (API `/items`, paneles coordinados, pasos en pantallas estrechas, paginación y selección múltiple).
- [x] **Fase 6: Motor de Transferencias Real** (Rclone/MEGAcmd, Celery, manifiesto SMB, cancelación y reintentos).
- [x] **Fase 7: Telemetría SSE & Notificaciones** (Redis Pub/Sub, progreso, administración SMTP y tareas de correo).
- [ ] **Fase 8: Auditoría, Hardening & Criterios de Aceptación** (auditoría de aplicación implementada; cerrar rate limiting, inmutabilidad en BD si se mantiene como requisito y evidencia de aceptación/E2E).

Las fases marcadas indican capacidades que existen en el repositorio; no certifican que el último commit pase CI ni que los proveedores estén accesibles desde cada despliegue. Consultar `docs/implementation-status.md` antes de afirmar que una entrega está validada.
