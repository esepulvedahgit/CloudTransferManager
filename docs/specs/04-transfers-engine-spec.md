# Especificación 04: Motor de Transferencias con MEGAcmd, Rclone y Seafile API

## 1. Contexto y Objetivos
El motor de transferencias es el núcleo operativo de Cloud Transfer Manager. Coordina copias asíncronas entre proveedores: **MEGAcmd** opera MEGA, **Rclone** opera Google Drive y Seafile con `adapter_type=RCLONE`, y el adaptador HTTPX de Seafile opera únicamente las conexiones marcadas explícitamente `adapter_type=SEAFILE_API`. Administra la cola, progreso, cancelaciones y reintentos.

---

## 2. Invocación Segura de los motores (Invariante Crítico)

### Reglas Obligatorias:
1. **Prohibición Total de Shell:** Rclone y MEGAcmd **NUNCA** se ejecutan mediante shell; ambos reciben argumentos separados. El login MEGA se envía por stdin a `mega-cmd`, sin contraseña en argv.
2. **Lista Blanca de Rclone:** Rclone se invoca solo con:
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
   - `copy`: Operación de copiado de archivos.
   - `lsjson`: Listado detallado de elementos en JSON.
   - `about`: Consulta de cuota y espacio.
   - `size`: Cálculo de tamaño total y cantidad de archivos.
3. **Lista Blanca de MEGAcmd:** Solo `mega-ls`, `mega-df`, `mega-get`, `mega-put -c`, `mega-logout` y `mega-quit`.
4. **Comandos Estrictamente Prohibidos:**
   - `delete`, `purge`, `sync`, `move`, `moveto`, `rmdir`, `dedupe`, `serve`, `mount`, `config dump`.
5. **Archivo de Configuración Temporal Efímero para Rclone:**
- Para cada trabajo, el worker genera un archivo de configuración temporal en `/var/lib/cloud-transfer/tmp/{job_id}.rclone.conf`.
   - Permisos estrictos: `0600` (solo legible por el usuario del worker).
- Contiene únicamente las secciones temporales de los remotos Rclone involucrados (uno para una transferencia MEGA↔Drive, dos para Drive↔Drive).
   - **Cláusula `finally` garantizada:** El archivo se elimina de forma segura e inmediata al terminar, fallar o cancelarse el trabajo.
- El directorio temporal se monta como `tmpfs`. Un bloqueo exclusivo del archivo evita ejecuciones concurrentes del mismo trabajo y permite que una redelivery reemplace y limpie un archivo huérfano dejado por una terminación abrupta.
6. **Staging por archivo:** Las copias cruzadas descargan o copian un solo archivo a un directorio privado, disk-backed y temporal. El archivo y su directorio se eliminan en `finally` antes de procesar el siguiente; el staging no es volumen persistente ni `tmpfs`. Se comprueba espacio libre cuando se conoce el tamaño.
7. **Selección Seafile:** La conexión guarda `RCLONE` o `SEAFILE_API`. Un error de autenticación, lectura o escritura no provoca fallback ni reautenticación silenciosa con otro adaptador.
8. **Integridad Seafile API:** Cada archivo subido mediante Seafile API se lee de vuelta en streaming y su SHA-256 se compara con el archivo local antes de incrementar el progreso completado. La verificación añade una lectura completa del destino.
9. **Conflictos en destino MEGA:** Antes de subir se consulta la ruta exacta; si ya existe, se omite y se suma una advertencia. Nunca se sobrescribe ni elimina el elemento remoto.
10. **Fallos de limpieza:** No se silencian errores al borrar staging. Si no se puede limpiar un archivo temporal, el trabajo falla y no procesa el siguiente. El HOME de MEGAcmd se retira solo después de confirmar `mega-quit`; si no se logra detener el daemon, se conserva el HOME y se falla de forma saneada.
11. **Listados completos:** Si la salida de `mega-ls` supera el límite de captura, el listado falla explícitamente; no se usa una lista parcial para navegar ni para declarar completa una transferencia.

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
- `RUNNING`: Proceso MEGAcmd o Rclone activo transfiriendo datos.
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
- Se aplican a los pasos de lectura/descarga y copia Drive de cada archivo MEGA↔Drive. MEGAcmd clasifica el fallo a partir de su salida internamente, pero no expone esa salida en errores ni logs.
- Para Seafile API, los timeouts/desconexiones y HTTP 408, 429 o 5xx configurados como transitorios se reintentan dentro del límite global. Las cargas reanudan desde el offset reportado por Seafile; una subida API a destino solo se declara completada tras verificar su SHA-256 de lectura posterior. Errores de autenticación y otros 4xx no se reintentan como fallos transitorios.
- En Seafile API con `SKIP`, el worker solicita `replace=false`; si un archivo aparece en el destino después de la comprobación inicial, Seafile puede crear un nombre único. El worker verifica la ruta y el SHA-256 que devolvió el servidor y falla explícitamente si el nombre no coincide, sin sobrescribir ni registrar esa copia como completada en la ruta solicitada.
- **Excepción de seguridad para subir a MEGA:** Una vez invocado `mega-put`, cualquier resultado fallido es ambiguo: el servidor pudo haber recibido el archivo aunque el cliente informe error. El trabajo no reintenta esa subida, consulta el destino solo para diagnóstico y registra `MEGA_DESTINATION_STATE_UNCERTAIN`; el operador debe verificar el destino antes de reintentar manualmente. Esto evita crear duplicados y no elimina ningún objeto remoto.
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
