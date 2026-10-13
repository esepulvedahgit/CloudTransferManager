# Plan: progreso SMB correcto y transferencias programadas en segundo plano

> **Para agentes que implementen este plan:** SUBHABILIDAD OBLIGATORIA: usar `superpowers:executing-plans` y completar cada tarea en orden. Mantener el trabajo en la rama actual de transferencias programadas y conservar los cambios SMB ajenos a este alcance.

**Objetivo:** Mostrar el avance y los fallos de copias SMB de forma precisa, y mantener las transferencias administradas por **Programaciones** en una vista compacta hasta que alcancen un estado terminal.

**Arquitectura:** Reutilizar el manifiesto SMB para conocer el resultado de cada archivo y calcular sus totales en una consulta agrupada, sin agregar columnas duplicadas. Incorporar filtros a la API existente de transferencias. Mostrar el total y el avance en un indicador compacto del encabezado, conservar SSE para las actualizaciones en vivo y usar una consulta cada 15 segundos como recuperación.

**Tecnologías:** FastAPI, SQLAlchemy async, PostgreSQL, manifiesto SMB y Celery Scheduler existentes, React, TypeScript, TanStack Query, MUI, SSE, pytest y Vitest.

**Especificaciones:** `docs/specs/04-transfers-engine-spec.md`, `docs/specs/05-realtime-telemetry-spec.md` y `docs/integrations/transfer-scheduling.md`.

## Reproducción verificada

La transferencia `TRF-2026-000032` terminó `FAILED` con `progress_percent=100`, aunque el manifiesto registró 26 archivos copiados y 1 fallido. Reportó 22.780.470 bytes copiados de 23.613.382 totales; la diferencia de 832.912 bytes coincide con el archivo fallido. `files_completed=27` cuenta archivos resueltos (copiados, omitidos o fallidos), por lo que no representa archivos copiados con éxito. El evento guardó únicamente `exception_type`, así que no es posible recuperar el código de salida de Rclone ni saber si el error era reintentable. La causa del 100% fue que la finalización de SMB fuerza ese valor después de leer el manifiesto.

La interfaz consulta la lista general cada tres segundos, mezcla tareas programadas con las transferencias activas y el historial, y puede promover una tarea `QUEUED` programada a la tarjeta grande de progreso. Por eso una tarea puede parecer activa antes de comenzar.

## Restricciones

- Las transferencias siguen siendo exclusivamente `COPY`; nunca borrar, mover ni modificar archivos de origen.
- Invocar Rclone como lista de argumentos y con `shell=False`; permitir solo `lsjson`, `copy`, `about` y `size`.
- No guardar stderr crudo de Rclone, credenciales ni rutas completas del proveedor en detalles técnicos. Se permiten datos seguros, como código de retorno y posibilidad de reintento.
- El frontend recibe estados y contadores numéricos, nunca credenciales guardadas.
- PostgreSQL, Redis, workers, control-worker y Beat permanecen en la red interna de Docker.
- Reutilizar manifiesto, SSE, scheduler y componentes MUI; no agregar dependencias ni contadores duplicados en la base de datos.
- Escribir una prueba que falle antes de cada cambio de comportamiento y pasarla antes de seguir a la siguiente tarea.

## Criterios de revisión

1. `progress_percent` de SMB representa bytes copiados con éxito. Un archivo fallido u omitido no permite que llegue a 100, incluso si su tamaño es cero; el estado terminal indica que el procesamiento terminó.
2. `files_completed` significa archivos resueltos, no archivos copiados. API e interfaz usan el manifiesto para mostrar copiados, fallidos, omitidos y pendientes.
3. El filtro de segundo plano reutiliza el criterio actual de **Programaciones** (`scheduled_at`, `wait_reason`, modo `EXCLUSIVE` o dependencia) e incluye estados no terminales. Excluye `COMPLETED`, `COMPLETED_WITH_WARNINGS`, `FAILED` y `CANCELLED`.
4. Las consultas de segundo plano conservan el filtro de propietario.
5. Si se pierde un evento SSE con el popover cerrado, la actualización de 15 segundos retira la tarea terminada del indicador y la incorpora al historial.

## Archivos involucrados

- `backend/app/services/transfer_executor.py`: avance SMB final y diagnósticos seguros de fallos por archivo.
- `backend/app/services/database_store.py`, `backend/app/services/memory_store.py`: filtros de tareas de segundo plano y resumen del manifiesto.
- `backend/app/main.py`: filtros y resúmenes en la API actual de transferencias.
- `frontend/src/App.tsx`: indicador compacto, suscripción SSE e historial separado.
- `frontend/src/styles.css`: estilo del indicador y popover.
- `backend/tests/test_smb_transfer_executor.py`, `backend/tests/test_database_store.py`, `backend/tests/test_smb_manifest.py`, `backend/tests/test_transfer_scheduling.py`: contabilidad, ambos stores, filtros y autorización.
- `frontend/src/App.test.tsx`: indicador, progreso, visibilidad del historial y finalización.
- `docs/specs/04-transfers-engine-spec.md`, `docs/specs/05-realtime-telemetry-spec.md`, `docs/api/openapi-contracts.md`, `docs/integrations/transfer-scheduling.md`: semántica del progreso, API y comportamiento visual.

## Task 1 / Tarea 1: Corregir el progreso SMB y los diagnósticos de fallo

**Archivos:** `backend/app/services/transfer_executor.py` y `backend/tests/test_smb_transfer_executor.py`.

1. Agregar `test_smb_failed_manifest_item_does_not_report_100_percent`: un archivo copiado y otro fallido deben terminar `FAILED`, con bytes copiados menores que el total y avance menor a 100. Incluir un caso con archivo fallido de cero bytes.
2. Agregar `test_smb_skipped_manifest_item_reports_warning_summary`: el archivo omitido mantiene `COMPLETED_WITH_WARNINGS`, no suma bytes copiados y no permite un avance de bytes copiados igual a 100.
3. Ejecutar la prueba enfocada y confirmar que falla:

   ```bash
   cd backend && .venv/bin/pytest -q tests/test_smb_transfer_executor.py -k 'failed_manifest_item or skipped_manifest_item'
   ```

4. En `_run_smb_transfer`, calcular el avance final con `bytes_transferred / bytes_total`. Limitarlo a 99,99 cuando haya archivos fallidos u omitidos, incluso si son de cero bytes. Usar 100 solo si todos los archivos del manifiesto se copiaron; conservar el comportamiento actual para manifiestos vacíos. Mantener la decisión de estado `FAILED` / `COMPLETED_WITH_WARNINGS` / `COMPLETED`. Tratar `files_completed` como archivos resueltos y usar el manifiesto para contar copiados.
5. En el evento `SMB_FILE_COPY_FAILED`, guardar `return_code` y `retryable` cuando el error sea `RcloneCommandError`; nunca guardar stderr ni la representación textual de la excepción.
6. Agregar `test_smb_copy_failure_records_safe_rclone_diagnostics` y verificar todo el archivo:

   ```bash
   cd backend && .venv/bin/pytest -q tests/test_smb_transfer_executor.py
   ```

## Task 2 / Tarea 2: Extender la API para las transferencias de segundo plano

**Archivos:** stores de base de datos y memoria, `backend/app/main.py`, pruebas de stores y `docs/api/openapi-contracts.md`.

1. Agregar `test_database_store_aggregates_manifest_summaries_for_transfer_page` con archivos copiados, fallidos, omitidos y pendientes; verificar que las entradas de directorio no se cuentan como archivos. Añadir una prueba equivalente de agregación para `MemoryStore` en `test_smb_manifest.py`.
2. Agregar `test_background_only_returns_nonterminal_scheduled_transfers`: cubrir los estados `SCHEDULED`, `QUEUED`, `VALIDATING`, `RUNNING`, `RETRYING` y `CANCELLING`; cada campo del criterio de **Programaciones**, incluidos trabajos `QUEUED` con `wait_reason` y sin `scheduled_at`; los cuatro estados terminales; paginación, resúmenes y aislamiento por propietario. Verificar también que `exclude_background=true` excluya solo tareas de Programaciones no terminales y conserve las terminadas.
3. Ejecutar las pruebas enfocadas y confirmar que fallan por la falta de los filtros/resúmenes:

   ```bash
   cd backend && .venv/bin/pytest -q tests/test_database_store.py::test_database_store_aggregates_manifest_summaries_for_transfer_page tests/test_transfer_scheduling.py::test_background_only_returns_nonterminal_scheduled_transfers
   ```

4. Implementar en ambos stores `alist_transfer_manifest_summaries(transfer_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]`, con consulta agrupada para los IDs de la página. Devolver `copied`, `failed`, `skipped` y `pending`, contando solo archivos (`is_dir=false`).
5. Extender `alist_transfers` con `background_only: bool = False` y `exclude_background: bool = False`. Reutilizar el criterio existente de Programaciones: `scheduled_at IS NOT NULL OR wait_reason IS NOT NULL OR execution_mode = 'EXCLUSIVE' OR depends_on_transfer_id IS NOT NULL`, junto con los seis estados no terminales. Aplicar el filtro de usuario antes de paginar. Mantener `pending_only` compatible.
6. Incorporar a `GET /api/v1/transfers` los parámetros `background_only` y `exclude_background`, `total_count` y `manifest_summary` en las filas de la página. Documentar el contrato. No se requiere migración de base de datos.
7. Ejecutar la suite enfocada:

   ```bash
   cd backend && .venv/bin/pytest -q tests/test_database_store.py tests/test_transfer_scheduling.py tests/test_smb_manifest.py
   ```

## Task 3 / Tarea 3: Mostrar el trabajo programado en un indicador compacto

**Archivos:** `frontend/src/App.tsx`, `frontend/src/styles.css` y `frontend/src/App.test.tsx`.

1. Agregar pruebas para verificar que: las tareas futuras no aparezcan en las tarjetas grandes ni en el historial; el indicador cuente tareas pendientes y en ejecución; el popover muestre el motivo de espera o los bytes transferidos; un evento SSE terminal retire la tarea del indicador y muestre su estado final en el historial; y la consulta de recuperación actualice la pantalla si se pierde el SSE.
2. Ejecutar las pruebas nuevas y confirmar que fallan:

   ```bash
   cd frontend && npm test -- --run -t 'background transfer|scheduled transfer history'
   ```

3. Añadir `Badge`, `IconButton` y `Popover` de MUI. Consultar `background_only=true` y usar `total_count` para el indicador, incluso si el popover carga solo la primera página. Mostrar motivo de espera en tareas en cola y avance en bytes para tareas activas. Reutilizar paleta, espaciado e iconografía existentes, con un indicador discreto. Anclar el origen del popover al botón y usar una transición breve de opacidad/transformación (150–200 ms), respuesta sutil al pulsar y respeto de `prefers-reduced-motion`.
4. Incorporar las transferencias de segundo plano activas al conjunto de suscripciones SSE, actualizar su caché con cada evento e invalidar las consultas de segundo plano y lista general al llegar a un estado terminal. Configurar la consulta general, pendientes y segundo plano para actualizarse cada 15 segundos, sin polling en pestañas de fondo.
5. Consultar la lista general con `exclude_background=true`; así las tareas de Programaciones no ocupan el espacio de la lista ni ocultan transferencias históricas más antiguas por paginación. Evitar que la promoción automática de `activeTransfer` abra una tarjeta grande para trabajos que cumplen el criterio de Programaciones. Mantener las acciones de reprogramar y cancelar en **Programaciones**. Al terminar, la tarea sale del indicador y aparece en la lista con su estado terminal.
6. En el historial SMB mostrar los contadores `copied`, `failed` y `skipped` del manifiesto. No etiquetar `files_completed` como archivos copiados.
7. Ejecutar toda la suite del frontend:

   ```bash
   cd frontend && npm test -- --run && npm run lint && npm run build
   ```

## Task 4 / Tarea 4: Documentar y validar

1. Actualizar las especificaciones de motor y telemetría, el contrato API y la guía de programación. Documentar que el avance SMB representa bytes copiados, que `files_completed` representa elementos resueltos, cómo se interpretan los cuatro contadores del manifiesto y que el trabajo programado no aparece en el historial hasta alcanzar un estado terminal.
2. Ejecutar verificaciones de backend:

   ```bash
   cd backend && .venv/bin/pytest -v --tb=short && .venv/bin/ruff check app tests && .venv/bin/mypy app
   ```

3. Validar y actualizar la instancia de Compose:

   ```bash
   docker compose config --quiet && docker compose up -d --build && docker compose ps
   ```

   Confirmar HTTP 200 en `/health/ready`, HTTP 200 en el frontend por el puerto HTTPS configurado y que scheduler/workers estén activos. Mantener los cambios en la rama actual; no integrar ni publicar como parte de este plan.
