# Especificación 04: Motor de Transferencias y Ejecución Segura de Rclone

## 1. Contexto y Objetivos
El motor de transferencias es el núcleo operativo de Cloud Transfer Manager. Coordina la ejecución asíncrona de transferencias entre proveedores mediante **Rclone**, administrando la cola de trabajos, monitorizando el progreso en tiempo real, gestionando cancelaciones y aplicando reintentos automáticos para errores transitorios.

---

## 2. Invocación Segura de Rclone (Invariante Crítico)

### Reglas Obligatorias:
1. **Prohibición Total de Shell:** Rclone **NUNCA** debe ejecutarse con `shell=True`. Se invoca estrictamente pasando una lista de argumentos (`list[str]`):
   ```python
   # Correcto:
   proc = await asyncio.create_subprocess_exec(
       settings.RCLONE_BINARY,
       "copy",
       f"{src_remote}:{src_path}",
       f"{dst_remote}:{dst_path}",
       "--config", temp_config_path,
       "--use-json-log",
       "--stats", "1s",
       "--stats-log-level", "NOTICE",
       stdout=asyncio.subprocess.PIPE,
       stderr=asyncio.subprocess.PIPE
   )
   ```
2. **Lista Blanca de Comandos Permitidos:**
   - `copy`: Operación de copiado de archivos.
   - `lsjson`: Listado detallado de elementos en JSON.
   - `about`: Consulta de cuota y espacio.
   - `size`: Cálculo de tamaño total y cantidad de archivos.
3. **Comandos Estrictamente Prohibidos (Blacklist Activa):**
   - `delete`, `purge`, `sync`, `move`, `moveto`, `rmdir`, `dedupe`, `serve`, `mount`, `config dump`.
4. **Archivo de Configuración Temporal Efímero:**
   - Para cada trabajo, el worker genera un archivo de configuración temporal en `/var/lib/cloud-transfer/tmp/{job_id}.rclone.conf`.
   - Permisos estrictos: `0600` (solo legible por el usuario del worker).
   - Contiene únicamente las secciones temporales de los dos remotos involucrados.
   - **Cláusula `finally` garantizada:** El archivo se elimina de forma segura e inmediata al terminar, fallar o cancelarse el trabajo.

---

## 3. Máquina de Estados de Transferencia

```
       [CREADO]
          |
          v
      +--------+
      | QUEUED |
      +--------+
          | (Worker toma el trabajo)
          v
    +------------+
    | VALIDATING | -----> [FAILED] (Ruta o credencial inválida)
    +------------+
          | (Validación exitosa)
          v
     +---------+
+--> | RUNNING | -----> [CANCELLING] -----> [CANCELLED]
|    +---------+
|         |
|         +-----------> [COMPLETED]
|         |
|         +-----------> [COMPLETED_WITH_WARNINGS]
|         |
|         v (Error transitorio)
|    +----------+
+--- | RETRYING | -----> [FAILED] (Excedió máx. 5 intentos)
     +----------+
```

### Estados y Semántica:
- `QUEUED`: En cola esperando worker disponible.
- `VALIDATING`: Verificando existencia de origen y permisos de destino.
- `RUNNING`: Proceso Rclone activo transfiriendo datos.
- `CANCELLING`: Solicitud de cancelación recibida; enviando SIGTERM/SIGKILL al subproceso.
- `CANCELLED`: Trabajo cancelado por el usuario; subproceso terminado y recursos liberados.
- `COMPLETED`: Todos los elementos copiados con éxito.
- `COMPLETED_WITH_WARNINGS`: Transferencia finalizada, pero algunos archivos no esenciales tuvieron advertencias.
- `FAILED`: Error fatal o superado el límite de reintentos.
- `RETRYING`: En espera de backoff exponencial para reintentar.

---

## 4. Políticas de Conflicto de Archivos

- `SKIP`: Si el archivo ya existe en destino, no se copia ni se sobrescribe.
- `OVERWRITE_IF_DIFFERENT` (Por defecto): Se copia si el tamaño o fecha de modificación difieren.
- *(Nota MVP: Operaciones que renombran o sobrescriben incondicionalmente sin verificar quedan fuera de esta versión).*

---

## 5. Estrategia de Reintentos Automáticos

- **Errores Reintentables:**
  - Timeouts de red y desconexiones de socket.
  - Respuestas HTTP 429 (Rate Limit / Quota transitoria).
  - Respuestas HTTP 5xx temporales del proveedor.
- **Errores No Reintentables (Fallo inmediato):**
  - Credenciales inválidas (`Authentication Failed`).
  - Permisos insuficientes permanentes (`Permission Denied`).
  - Ruta de origen inexistente (`File Not Found`).
  - Cancelación manual solicitada por el usuario.
- **Estrategia Backoff:**
  - Intentos máximos: 5 (`TRANSFER_MAX_RETRIES=5`).
  - Fórmula: `Espera = min(5 * (2 ** (intento - 1)) + jitter, 300)` segundos.

---

## 6. Esquema de Base de Datos para Trabajos (`transfer_jobs`)

```sql
CREATE TABLE transfer_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    public_id VARCHAR(32) UNIQUE NOT NULL,
    requested_by UUID NOT NULL REFERENCES users(id),
    source_connection_id UUID NOT NULL REFERENCES storage_connections(id),
    destination_connection_id UUID NOT NULL REFERENCES storage_connections(id),
    source_paths JSONB NOT NULL,
    destination_path VARCHAR(1024) NOT NULL,
    operation VARCHAR(16) NOT NULL DEFAULT 'COPY',
    conflict_policy VARCHAR(32) NOT NULL DEFAULT 'OVERWRITE_IF_DIFFERENT',
    status VARCHAR(32) NOT NULL DEFAULT 'QUEUED',
    progress_percent NUMERIC(5, 2) NULL,
    files_total BIGINT NULL,
    files_completed BIGINT NOT NULL DEFAULT 0,
    bytes_total BIGINT NULL,
    bytes_transferred BIGINT NOT NULL DEFAULT 0,
    current_item VARCHAR(1024) NULL,
    average_speed_bps BIGINT NULL,
    estimated_remaining_seconds BIGINT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 1,
    error_count INTEGER NOT NULL DEFAULT 0,
    warning_count INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMP WITH TIME ZONE NULL,
    completed_at TIMESTAMP WITH TIME ZONE NULL,
    cancel_requested_at TIMESTAMP WITH TIME ZONE NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_transfer_jobs_status ON transfer_jobs(status);
CREATE INDEX idx_transfer_jobs_requested_by ON transfer_jobs(requested_by);
```

---

## 7. Estrategia de Pruebas (TDD)
- **Unitarias:**
  - `test_rclone_command_builder`: Valida que los argumentos se armen estrictamente como lista y sin flags no permitidas.
  - `test_rclone_forbidden_commands_raise`: Pasar comandos como `delete` o `sync` levanta excepción inmediata.
  - `test_state_machine_transitions`: Transiciones inválidas (ej. `COMPLETED -> RUNNING`) son rechazadas.
  - `test_ephemeral_config_deleted_on_exception`: Verifica que el archivo temporal se borre incluso si el subproceso falla con excepción.
- **Integración:**
  - `test_celery_task_cancel_terminates_subprocess`: Enviar cancelación termina el proceso Rclone y pasa el estado a `CANCELLED`.
