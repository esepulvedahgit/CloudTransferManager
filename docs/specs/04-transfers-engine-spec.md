# Especificación 04: Motor de Transferencias con MEGAcmd, Rclone, Seafile API y Nextcloud

## 1. Contexto y Objetivos
El motor de transferencias es el núcleo operativo de Cloud Transfer Manager. Coordina copias asíncronas entre proveedores: **MEGAcmd** opera MEGA; **Rclone** opera Google Drive, Nextcloud WebDAV y Seafile con `adapter_type=RCLONE`; el adaptador HTTPX de Seafile opera únicamente las conexiones marcadas explícitamente `adapter_type=SEAFILE_API`. Administra la cola, progreso, cancelaciones y reintentos. Los servidores Nextcloud se registran desde la UI como conexiones; su perfil está descrito en `docs/integrations/nextcloud-compatibility.md`.

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
3. **Lista Blanca de MEGAcmd:** Solo `mega-ls`, `mega-df`, `mega-get`, `mega-put -c`, `mega-mkdir -p` (para estructura de destino), `mega-logout` y `mega-quit`.
4. **Comandos Estrictamente Prohibidos:**
   - `delete`, `purge`, `sync`, `move`, `moveto`, `rmdir`, `dedupe`, `serve`, `mount`, `config dump`.
5. **Archivo de Configuración Temporal Efímero para Rclone:**
- Para cada trabajo, el worker genera un archivo de configuración temporal en `/var/lib/cloud-transfer/tmp/{job_id}.rclone.conf`.
   - Permisos estrictos: `0600` (solo legible por el usuario del worker).
- Contiene únicamente las secciones temporales de los remotos Rclone involucrados (uno para una transferencia MEGA↔Drive, dos para Drive↔Drive).
   - **Cláusula `finally` garantizada:** El archivo se elimina de forma segura e inmediata al terminar, fallar o cancelarse el trabajo.
- El directorio temporal se monta como `tmpfs`. Un bloqueo exclusivo del archivo evita ejecuciones concurrentes del mismo trabajo y permite que una redelivery reemplace y limpie un archivo huérfano dejado por una terminación abrupta.
6. **Staging por archivo:** Las transferencias nuevas descargan o copian un solo archivo a un directorio privado, disk-backed y temporal. El archivo y su directorio se eliminan en `finally` antes de procesar el siguiente; el staging no es volumen persistente ni `tmpfs`. Antes de escribir se reserva espacio para el archivo mayor conocido y se vuelve a comprobar por archivo. Los documentos nativos Google sin tamaño se limitan a 64 MiB o al límite global inferior.
7. **Semántica uniforme de carpetas:** Al seleccionar una carpeta, se crea en el destino una carpeta con el mismo nombre y se copia dentro todo su contenido, conservando subcarpetas y carpetas vacías. Los archivos seleccionados individualmente se copian directamente a la ruta destino. Esto aplica a todas las combinaciones de proveedores y adaptadores; que el motor procese los archivos uno a uno no cambia la estructura final.
8. **Selección Seafile:** La conexión guarda `RCLONE` o `SEAFILE_API`. Un error de autenticación, lectura o escritura no provoca fallback ni reautenticación silenciosa con otro adaptador.
9. **Integridad Seafile API:** Cada archivo subido mediante Seafile API se lee de vuelta en streaming y su SHA-256 se compara con el archivo local antes de incrementar el progreso completado. La verificación añade una lectura completa del destino.
10. **Conflictos en destino:** Las transferencias nuevas enumeran nombres por carpeta y asignan una copia `_01`, `_02`, etc. cuando el nombre pedido está ocupado. Nunca sobrescriben ni eliminan el elemento remoto. Los trabajos históricos mantienen su política guardada.
11. **Fallos de limpieza:** No se silencian errores al borrar staging. Si no se puede limpiar un archivo temporal, el trabajo falla y no procesa el siguiente. El HOME de MEGAcmd se retira solo después de confirmar `mega-quit`; si no se logra detener el daemon, se conserva el HOME y se falla de forma saneada.
12. **Listados completos:** Si la salida de `mega-ls` supera el límite de captura, el listado falla explícitamente; no se usa una lista parcial para navegar ni para declarar completa una transferencia.
13. **Preevaluación acotada:** El POST valida permisos y forma de las rutas y responde sin consultar los proveedores. La validación remota e inventario ocurren en el worker antes de copiar. Se aceptan hasta 100 rutas por solicitud; el máximo es `SMB_TRANSFER_MAX_FILES` para origen SMB y `TRANSFER_MAX_FILES` para los demás orígenes. `TRANSFER_MAX_FILE_BYTES=0` desactiva el límite por archivo; un valor positivo lo activa. Un archivo de tamaño desconocido falla antes de escribir salvo exportaciones de documentos nativos Google, que se limitan a 64 MiB o al máximo global inferior. El inventario es previo a la copia y no constituye una instantánea del origen; cambios concurrentes quedan sujetos a una preevaluación de mejor esfuerzo.
14. **Progreso e integridad:** El progreso cuenta bytes lógicos del archivo una sola vez y el tamaño local se compara con el origen. La carga Seafile API se verifica por SHA-256; Rclone confirma su operación y tamaño; MEGA confirma la ruta y tamaño. Las escrituras ambiguas usan lectura posterior y SHA-256 antes de reanudar.
15. **Progreso SMB parcial:** La actualización tras cada archivo y el cierre usan bytes copiados, no archivos solo resueltos. Si existen elementos fallidos, omitidos o pendientes, el porcentaje queda por debajo de 100%, incluso cuando esos elementos tienen tamaño cero; para árboles sin bytes conocidos, el respaldo cuenta únicamente archivos copiados.
16. **Resultado de inventario SMB:** `files_completed` representa elementos del inventario ya resueltos; puede incluir elementos `COPIED`, `SKIPPED` o `FAILED` y por sí solo no equivale a archivos copiados. El manifiesto es la fuente de verdad para los contadores `copied` (copiados y verificados), `failed` (con error de copia/verificación), `skipped` (omitidos por conflicto) y `pending` (aún no resueltos); los cuatro cuentan solo archivos y excluyen directorios. El porcentaje final SMB se calcula con los bytes copiados; si el total de bytes es cero, usa la proporción de archivos copiados. Si existe algún fallo, omisión o elemento pendiente, el porcentaje se limita a menos de 100 aunque todos los elementos estén resueltos. Una transferencia con elementos fallidos termina en `FAILED`; una con solo omisiones termina en `COMPLETED_WITH_WARNINGS`.
17. **Diagnósticos seguros:** Los eventos guardan etapa, intento, nombre base del archivo cuando existe, tipo de excepción y ubicación de código acotada. Los fallos individuales SMB pueden incluir código de salida y si el error es reintentable. Las copias Rclone directas incluyen estados HTTP y categorías de operación/error de una lista fija. La captura de salida de copia conserva los últimos 64 KiB para retener el error terminal de trabajos largos. No guardan texto crudo de stderr/excepción ni rutas del proveedor en los detalles técnicos.
18. **Nombres versionados universales:** Las transferencias nuevas de todos los pares reales admitidos usan `KEEP_BOTH`. Un nombre ocupado en destino recibe `_01`, `_02`, etc., antes de la extensión; se compara NFC sin distinguir mayúsculas, sin leer el contenido existente. La asignación, identidad del objeto de origen, nombre solicitado y nombre final se guardan en el manifiesto común antes de escribir. Las copias se procesan por archivo con staging; Rclone usa `--ignore-existing`, Seafile API usa `replace=false` y MEGA conserva handles de origen. Reintentos y reanudaciones reutilizan la asignación. Una escritura ambigua solo se acepta si se demuestra igualdad SHA-256; de lo contrario el trabajo se detiene sin cambiar a otro nombre ni reintentar a ciegas. La ruta MOCK continúa simulando progreso y no modela escrituras ni colisiones.
19. **Resultado con numeración:** Cada asignación renombrada añade una advertencia al historial. Si las copias terminan y hubo numeración, el estado es `COMPLETED_WITH_WARNINGS`. El contenido idéntico no evita la numeración.

---

## 3. Máquina de Estados de Transferencia

Las transferencias nuevas de todos los proveedores se inventarían y ejecutan por archivo para conservar la identidad de origen y fijar nombres antes de escribir. El manifiesto común guarda asignaciones y estados para archivos y carpetas; los trabajos históricos conservan la política anterior. Seafile API verifica cada escritura por SHA-256. Los documentos nativos Google sin tamaño se exportan una vez a staging con límite de 64 MiB o el límite global inferior y se contabilizan al conocer el tamaño.

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

Una transferencia programada pasa por `SCHEDULED` hasta `scheduled_at`, luego se vuelve `QUEUED` y sigue la máquina existente. `QUEUED` también representa trabajos bloqueados por capacidad, dependencia, conexión o staging; `wait_reason` explica el motivo.

Las tareas con fecha futura tienen un registro durable de evaluación anticipada. Sus estados `PENDING`, `EVALUATING`, `APPROVED`, `BLOCKED`, `ERROR` y `WAITING_PARENT` son independientes de la máquina de copia. El dispatcher no reserva una copia hasta `APPROVED`; las pendientes usan `WAITING_EVALUATION` y los resultados bloqueados usan `BLOCKED_EVALUATION`, sin cambiar la tarea a `FAILED`. La evaluación comparte el inventario de solo lectura con el ejecutor y nunca crea manifiesto, descarga contenido ni reserva staging. Al comenzar la copia, el ejecutor valida de nuevo el origen.

### Estados y Semántica:

- `SCHEDULED`: Espera su fecha de inicio. Beat recupera fechas vencidas después de una interrupción.
- `QUEUED`: En cola esperando capacidad, dependencias o disponibilidad de conexiones.
- `VALIDATING`: Verificando existencia de origen y permisos de destino.
- `RUNNING`: Proceso MEGAcmd o Rclone activo transfiriendo datos.
- `CANCELLING`: Solicitud de cancelación recibida; enviando SIGTERM/SIGKILL al subproceso.
- `CANCELLED`: Trabajo cancelado por el usuario; subproceso terminado y recursos liberados.
- `COMPLETED`: Todos los elementos copiados con éxito.
- `COMPLETED_WITH_WARNINGS`: Transferencia finalizada, pero algunos archivos no esenciales tuvieron advertencias.
- `FAILED`: Error fatal o superado el límite de reintentos.
- `RETRYING`: En espera de backoff exponencial para reintentar; registra `RETRY_SCHEDULED` con el próximo intento y el tiempo de espera.

---

## 4. Política de nombres en transferencias nuevas

- La API usa `KEEP_BOTH` cuando se omite `conflict_policy` y rechaza `SKIP` y `OVERWRITE_IF_DIFFERENT` en solicitudes nuevas.
- Los trabajos históricos mantienen la política almacenada. No se aplica una migración que cambie su significado.
- `file.txt` ocupado produce `file_01.txt`; si ese nombre también existe, se prueban `_02`, `_03` y siguientes, incluso después de 99. Se conservan extensiones y archivos ocultos. Los componentes largos se acortan respetando el límite del proveedor; si no cabe una extensión válida, se falla antes de escribir.
- Una carpeta de destino existente se reutiliza cuando es compatible. Una colisión carpeta/archivo se numera y el subárbol conserva esa asignación. También se conservan carpetas vacías.

---

## 5. Estrategia de Reintentos Automáticos

- **Errores Reintentables:**
  - Timeouts de red y desconexiones de socket.
  - Respuestas HTTP 429 (Rate Limit / Quota transitoria).
  - Respuestas HTTP 5xx temporales del proveedor.
- Si `mega-get` termina sin crear el archivo local esperado, el fallo se identifica como `MEGACMD_DOWNLOAD_OUTPUT_MISSING`, registra `SOURCE_DOWNLOAD`, código de salida 0 y `expected_output_created=false`, y se reintenta como máximo una vez. Este reintento ocurre antes de iniciar la escritura en destino y no excede el límite global configurado.
- Se aplican a los pasos de lectura/descarga y copia Drive de cada archivo MEGA↔Drive. MEGAcmd clasifica el fallo a partir de su salida internamente, pero no expone esa salida en errores ni logs. Los eventos de reintento indican el próximo intento del archivo actual.
- Para Seafile API, los timeouts/desconexiones y HTTP 408, 429 o 5xx configurados como transitorios se reintentan dentro del límite global. Las cargas reanudan desde el offset reportado por Seafile; una subida API a destino solo se declara completada tras verificar su SHA-256 de lectura posterior. Errores de autenticación y otros 4xx no se reintentan como fallos transitorios.
- En trabajos históricos Seafile API con `SKIP`, el worker solicita `replace=false` y verifica la ruta y el SHA-256 que devuelve el servidor. Las nuevas transferencias también solicitan `replace=false` y fallan si el proveedor no crea exactamente la ruta asignada.
- **Excepción de seguridad para subir a MEGA:** Una vez invocado `mega-put`, cualquier resultado fallido es ambiguo: el servidor pudo haber recibido el archivo aunque el cliente informe error. El trabajo no reintenta esa subida, consulta el destino solo para diagnóstico y registra `MEGA_DESTINATION_STATE_UNCERTAIN`; el operador debe verificar el destino antes de reintentar manualmente. Esto evita crear duplicados y no elimina ningún objeto remoto.
- **Errores No Reintentables (Fallo inmediato):**
  - Credenciales inválidas (`Authentication Failed`).
  - Permisos insuficientes permanentes (`Permission Denied`).
  - Ruta de origen inexistente (`File Not Found`).
  - Cancelación manual solicitada por el usuario.
- **Estrategia Backoff:**
- Hasta 5 reintentos después de la ejecución inicial (`TRANSFER_MAX_RETRIES=5`), es decir, hasta 6 intentos totales.
  - Fórmula: `Espera = min(5 * (2 ** (intento - 1)) + jitter, 300)` segundos.

---

## 6. Esquema de Base de Datos para Trabajos (`transfer_jobs`)

El esquema mantiene la fila de transferencia existente y añade estos campos para programar y admitir trabajos de forma durable:

```sql
scheduled_at TIMESTAMP WITH TIME ZONE NULL,
schedule_timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
execution_mode VARCHAR(16) NOT NULL DEFAULT 'AUTO', -- AUTO | EXCLUSIVE
depends_on_transfer_id UUID NULL REFERENCES transfer_jobs(id) ON DELETE RESTRICT,
wait_reason VARCHAR(64) NULL,
dispatch_token UUID NULL,
dispatch_claimed_at TIMESTAMP WITH TIME ZONE NULL,
execution_started_at TIMESTAMP WITH TIME ZONE NULL,
execution_heartbeat_at TIMESTAMP WITH TIME ZONE NULL
```

El dispatcher serializa admisiones con un advisory lock transaccional y reserva cada transferencia antes de publicarla a Celery. El worker valida el token y vuelve a comprobar estado, permisos y versión de credenciales antes de ejecutar. Beat revisa pendientes cada 15 segundos; un token sin inicio se libera tras 120 segundos y un heartbeat vencido solo se marca interrumpido si el advisory lock de ejecución ya no está tomado. Los mensajes de reserva usan UUID nuevos para descartar entregas antiguas.

`AUTO` respeta `TRANSFER_MAX_CONCURRENT`, las colisiones de lectura/escritura sobre conexiones y un cupo global de staging. Las transferencias nuevas `KEEP_BOTH` reservan staging cualquiera que sea el par de proveedores. `EXCLUSIVE` espera a que terminen los trabajos admitidos, levanta una barrera FIFO y evita admitir posteriores hasta finalizar. La programación no inicia una VPN ni expone el SMB: el worker debe tener conectividad de red vigente hacia el NAS.

Las transferencias programadas o en espera se consultan aparte de la lista de historial. `background_only=true` devuelve trabajos activos que tienen fecha, modo exclusivo, dependencia o motivo de espera; `exclude_background=true` los omite del historial hasta que alcanzan un estado terminal. La API de listados agrega `manifest_summary` para que el historial SMB muestre resultados reales en vez de presentar `files_completed` como cantidad copiada.

Los únicos padres que liberan un hijo son `COMPLETED` y `COMPLETED_WITH_WARNINGS`. `FAILED` y `CANCELLED` mantienen el hijo bloqueado para evitar una ejecución accidental.

El esquema completo conserva las columnas ya existentes:

```sql
CREATE TABLE transfer_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    public_id VARCHAR(32) UNIQUE NOT NULL,
    requested_by UUID NOT NULL REFERENCES users(id),
    source_connection_id UUID NOT NULL REFERENCES storage_connections(id),
    destination_connection_id UUID NOT NULL REFERENCES storage_connections(id),
    source_paths JSONB NOT NULL,
    source_item_ids JSONB NULL,
    source_parent_ids JSONB NULL,
    destination_path VARCHAR(1024) NOT NULL,
    operation VARCHAR(16) NOT NULL DEFAULT 'COPY',
    conflict_policy VARCHAR(32) NOT NULL DEFAULT 'KEEP_BOTH',
    status VARCHAR(32) NOT NULL DEFAULT 'QUEUED',
    scheduled_at TIMESTAMP WITH TIME ZONE NULL,
    schedule_timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
    execution_mode VARCHAR(16) NOT NULL DEFAULT 'AUTO',
    depends_on_transfer_id UUID NULL REFERENCES transfer_jobs(id) ON DELETE RESTRICT,
    wait_reason VARCHAR(64) NULL,
    dispatch_token UUID NULL,
    dispatch_claimed_at TIMESTAMP WITH TIME ZONE NULL,
    execution_started_at TIMESTAMP WITH TIME ZONE NULL,
    execution_heartbeat_at TIMESTAMP WITH TIME ZONE NULL,
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
CREATE INDEX ix_transfer_dispatch ON transfer_jobs(status, scheduled_at, created_at);
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

## Corrección y recuperación por archivo (2026-10-16)

El flujo KEEP_BOTH usa resultados internos `COPIED`, `FAILED_SAFE` y `STOPPED`. Un archivo ausente permite continuar solo si el proveedor confirma su ausencia: Google Drive se vuelve a consultar por ID y padre; Seafile API consulta la ruta en su directorio; Rclone vuelve a listar el padre sin caché y comprueba ruta/ID. Los 401/403, errores de red, permisos y comprobaciones inconclusas detienen el trabajo. Un tamaño cambiado y la salida MEGA ausente tras un reintento adicional pueden continuar antes de comenzar la escritura remota y tras limpiar staging. Errores desconocidos, autenticación/cuota, falta de espacio, limpieza fallida y proveedor indisponible detienen el trabajo. Tres fallos seguros consecutivos con el mismo código y etapa detienen con `TRANSFER_REPEATED_FILE_FAILURE`; una copia verificada o reconciliada por SHA-256 reinicia la cuenta. Si quedan fallidos o pendientes, termina FAILED y debajo de 100%, incluso con archivos vacíos. Reanudar omite COPIED y conserva asignaciones.

Una vez iniciada una subida de destino, no se repite automáticamente ante una excepción de Rclone, Seafile o MEGAcmd, incluso si todavía no aparece el objeto remoto. El worker busca el destino asignado y compara tamaño y SHA-256; si no puede demostrar que la escritura terminó correctamente, marca `DESTINATION_STATE_UNCERTAIN` (o `MEGA_DESTINATION_STATE_UNCERTAIN`) y se detiene. Así evita duplicar o sobrescribir datos por consistencia eventual o una respuesta perdida.

Un manifiesto MEGA previo cuyas identidades/rutas difieran del inventario actual se rechaza antes de escribir con `MEGA_MANIFEST_REBUILD_REQUIRED`. No se reparan nombres históricos automáticamente: se crea una nueva transferencia seleccionando de nuevo por identificador. Las carpetas creadas anteriormente se conservan.

Los eventos incluyen etapa (`SOURCE_DOWNLOAD`, `DESTINATION_UPLOAD`, `DESTINATION_VERIFY`), proveedor, intento y escritura iniciada. Los diagnósticos Rclone conservan códigos/categorías HTTP acotados, nunca stderr crudo. REST/SSE incluyen `manifest_summary` con `copied`, `failed`, `skipped`, `pending`, `renamed`; también se adjunta al primer snapshot SSE, incluso si ya es terminal. `renamed` cuenta asignaciones de archivos y carpetas. `files_completed` conserva su semántica histórica de elementos resueltos. La UI de proveedores reales usa el resumen para no presentar fallidos como copiados; MOCK sigue simulando progreso.
