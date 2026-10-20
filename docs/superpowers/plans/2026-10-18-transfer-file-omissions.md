# Omisión controlada de archivos — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. No delegar sin autorización del usuario.

**Goal:** Omitir archivos de origen mediante una decisión auditable o una restricción individual MEGA confirmada, continuar sin duplicar copias y comunicar las omisiones.

**Architecture:** Ampliar el manifiesto existente con motivos de omisión y evidencia de fallo; centralizar elegibilidad y transiciones transaccionales. Mantener el ejecutor por archivo y el despacho durable; añadir acciones de recuperación, advertencias y protección de dependencias sin cambiar los motores de copia.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL 16, Celery/Redis, React, TypeScript, Material UI y TanStack Query.

**Spec:** [Diseño para revisión](../specs/2026-10-18-transfer-file-omissions-design.md).

**Estado:** aprobado para implementación por el usuario; el despliegue y la recuperación de la transferencia real siguen pendientes de revisión explícita.

**Commits de la implementación:** `abfb928` (interfaz e informes), `7682f72` (diagnóstico conservador MEGA), `2904b08` (seguridad/idempotencia) y `5e57fbc` (documentación y validación).

## Global Constraints

- «Solo operación `COPY`. Nunca eliminar ni alterar archivos en el almacenamiento de origen».
- «NUNCA invocar Rclone mediante `shell=True`»; comandos permitidos: `lsjson`, `copy`, `about`, `size`.
- MEGA conserva MEGAcmd, sesión privada, login por stdin, limpieza supervisada y comandos permitidos del repositorio.
- Cero secretos en repositorio/logs. No persistir stdout/stderr crudos ni cambiar versiones de credenciales para recuperar un trabajo.
- «Reanudar omite COPIED y conserva asignaciones»; ampliar únicamente a omisiones explícitas del nuevo diseño, preservando `SKIPPED` históricos.
- «Las escrituras ambiguas usan lectura posterior y SHA-256 antes de reanudar»; no hacerlas omitibles.
- `source_file_error_policy = STOP | SKIP_CONFIRMED_RESTRICTED`, por defecto `STOP`.
- Implementación mediante ciclos red-green-refactor atómicos de 2–5 minutos. No avanzar con pruebas fallidas.
- No aplicar migraciones, modificar manifiestos reales, reiniciar servicios ni reanudar `TRF-2026-000013` durante la preparación/revisión del plan.

## Review Focus

1. Dos clics/reintentos HTTP o concurrencia con dispatcher no deben omitir dos veces ni abrir dos intentos: tareas 2 y 4, pruebas PostgreSQL reales.
2. Diagnóstico histórico de otro intento o basename repetido no identifica un archivo: tarea 2, fixtures negativas y caso del intento 2.
3. Worker que publica `FAILED` antes de terminar limpieza no permite recuperación mientras conserva ejecución/reserva: tareas 2 y 3.
4. Padres reintentados invalidan aceptaciones anteriores; evaluación y despacho no discrepan: tarea 4, carreras en PostgreSQL.
5. Un correo pendiente de un intento anterior no debe describir las omisiones del intento nuevo: tarea 5, snapshot antiguo y nuevo.

---

## Secuencia de entrega

**Entrega A:** tareas 1–6: omisión manual, continuidad, dependencias, advertencias, informe y reintento selectivo. Es utilizable sin clasificación automática y permite recuperar el caso actual por UI/API.

**Entrega B:** tarea 7: política automática opt-in, apoyada en los mismos controles. Solo habilitarla si existe evidencia verificable del diagnóstico individual de la versión instalada; si no existe, A sigue siendo funcional y B permanece conservadora en `UNKNOWN`.

**Cierre:** tarea 8: verificaciones, documentación y procedimiento de despliegue para revisión. Cada entrega requiere todos sus controles antes de publicarse.

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| `backend/app/models.py`, `services/memory_store.py`, `services/database_store.py` | Campos, DTOs, transacciones, contadores, auditoría e idempotencia. |
| `backend/migrations/versions/20261018_0015_transfer_file_omissions.py` (nuevo) | Migración aditiva con valores neutrales y constraints. Confirmar head antes de crear. |
| `backend/app/services/transfer_omissions.py` (nuevo) | Tipos, elegibilidad, compatibilidad histórica y decisiones puras. |
| `backend/app/services/transfer_recovery.py` (nuevo) | Orquestación compartida de autorización/recuperación, sin subprocess ni SQL fuera del store. |
| `backend/app/services/transfer_omission_report.py` (nuevo) | CSV seguro, paginado y limitado a datos públicos autorizados. |
| `backend/app/services/transfer_executor.py`, `transfer_outcomes.py`, `transfer_progress.py` | Continuidad, resultados, limpieza y cobertura de copia. |
| `backend/app/main.py` | DTOs/endpoints y conexión mínima con los servicios nuevos. |
| `backend/app/services/transfer_scheduling.py`, `transfer_evaluation.py`, `transfer_evaluator.py`, `worker.py` | Dependencias, despacho, snapshots de correo y reentregas. Modificar solo los puntos que consumen los datos nuevos. |
| `backend/app/services/megacmd.py` | Clasificación segura del diagnóstico de MEGAcmd en memoria. |
| `backend/app/services/notifications.py` | Advertencias/conteos desde snapshot terminal. |
| `frontend/src/TransferFileReport.tsx`, `TransferFileReport.test.tsx` (nuevos) | Extraer el informe existente y añadir omisión, CSV y reintento selectivo. |
| `frontend/src/transferTypes.ts` (nuevo), `App.tsx`, `App.test.tsx` | Tipos compartidos, política automática, historial, confirmaciones y bloqueo de hijos. |
| `backend/tests/test_transfer_omissions.py`, `test_transfer_recovery_api.py`, `test_transfer_recovery_store.py`, `test_transfer_omission_report.py` (nuevos) | Política, endpoints, concurrencia y exportación. |
| Pruebas existentes de executor, migrations, scheduling, evaluation, stream, worker, SMTP y MEGAcmd | Regresiones sobre los consumidores afectados. |

## Task 1: Persistencia y tipos de omisión

**Files:** modelos, stores, migración nueva, `transfer_omissions.py`; pruebas `test_migrations.py`, `test_transfer_recovery_store.py`.

**Interfaces:** enums `OmissionKind` y `SourceFileErrorPolicy` con los valores exactos del diseño. `TransferManifestItemRecord` expone los cinco campos nuevos del manifiesto; `TransferRecord` incorpora política y aceptación de padre. `manifest_summary` añade `omitted` y `omitted_bytes` conservando todas sus claves anteriores.

- [x] Escribir pruebas fallidas de defaults `STOP`/NULL, round-trip en ambos stores y upgrade desde `0014` sin alterar estados, bytes o `SKIPPED` históricos.
- [x] Ejecutar `cd backend && pytest tests/test_migrations.py tests/test_transfer_recovery_store.py -q`; confirmar los fallos por campos aún ausentes.
- [x] Implementar migración aditiva y DTOs; impedir clases desconocidas y omisiones explícitas con `copy_started=true`/directorio. Actor eliminado queda NULL sin perder el registro de la omisión. Mapear policy en creación y snapshots públicos; incluirla en el hash de idempotencia de creación.
- [x] Añadir agregación consistente: `omitted <= skipped`, bytes omitidos solo de omisiones explícitas; sin consultas por cada archivo/trabajo. Verificar nuevas pruebas y `test_smb_manifest.py`.
- [x] Revisar el diff y crear commit `feat: persist explicit transfer file omissions`.

## Task 2: Omisión manual y reanudación atómicas

**Files:** `transfer_omissions.py`, `transfer_recovery.py`, ambos stores, `main.py`; nuevas pruebas de política/API/store y `test_api_security.py`.

**Interfaces:** `SkipAndResumeRequest(expected_attempt: int, request_id: UUID, reason: Literal['USER_ACCEPTED_SOURCE_FAILURE'], source_file_error_policy: SourceFileErrorPolicy | None = None)`; `can_skip_manifest_item(transfer: TransferRecord, item: TransferManifestItemRecord, *, failure_stage: str | None, failure_attempt: int | None) -> bool`. Stores exponen `askip_transfer_item_and_resume(transfer_id: UUID, item_id: UUID, *, actor_id: UUID, expected_attempt: int, request_id: UUID, source_file_error_policy: str | None = None) -> TransferRecord`, con excepciones tipadas para conflicto/no visible. El servicio calcula/revalida evidencia y permisos dentro de la unidad transaccional del store.

- [x] Escribir pruebas fallidas: escenario saneado de TRF-2026-000013 convierte un `FAILED` a omisión, conserva 1.977 copiados/265.040.247.057 bytes, mantiene `files_completed=1978`, baja un error, deja 2.339 pendientes y sube el intento 2→3. Auditoría `TRANSFER_FILE_OMITTED` y `TRANSFER_RESUMED` en la misma transacción.
- [x] Añadir negativos: CSRF, usuario sin acceso, ID de otro trabajo, directorio, item ya copiado, destino iniciado/incierto, cuenta/cuota/limpieza, versión de credenciales distinta y reserva todavía activa. Fixture histórica con basename repetido, diagnóstico anterior al `started_at` o de etapa distinta debe rechazar, sin mutaciones.
- [x] Ejecutar `cd backend && pytest tests/test_transfer_omissions.py tests/test_transfer_recovery_api.py tests/test_transfer_recovery_store.py -q` y confirmar fallos iniciales.
- [x] Implementar endpoint `skip-and-resume`, validaciones transaccionales y eventos con `manifest_item_id`, intento global y motivo enum. Reutilizar el despacho posterior al commit y cancelación durable existentes. Añadir el cálculo backend de `can_skip`/motivo al informe.
- [x] Probar rollback si falla auditoría; dos llamadas iguales devuelven replay sin otro intento; mismo request con propósito diferente da `409`; carreras omitir/omitir y omitir/resume/dispatcher en PostgreSQL desechable no producen más de un intento nuevo. Usar la configuración de fixtures PostgreSQL existente, nunca la DB de instalación.
- [x] Pasar pruebas enfocadas y crear commit `feat: add audited skip and resume recovery`.

## Task 3: Ejecución y finalización respetando omisiones

**Files:** executor, outcomes, progress, stores; `test_transfer_executor.py`, `test_worker.py`, `test_staging.py`.

**Interfaces:** `CopyOutcome` añade `SKIPPED`; el ejecutor preserva `FAILED_SAFE` existente. Los fallos nuevos guardan `last_failure_stage`, `last_failure_attempt` y evento con UUID de entrada, `attempt` global y `file_attempt` interno.

- [x] Escribir pruebas fallidas: reanudar ignora `COPIED` y omisiones explícitas sin ignorar otros `FAILED`/`PENDING`; ninguna llamada download/upload para el omitido; conserva rutas de destino. Reentrega/reinicio mantiene la omisión. `SKIPPED` histórico conserva la conducta anterior.
- [x] Añadir finalización: 4.316 copiados/uno omitido → `COMPLETED_WITH_WARNINGS`; cualquier fallo pendiente conserva `FAILED`; bytes omitidos nunca se suman. Un omitido de cero bytes también impide mostrar cobertura 100%.
- [x] Ejecutar `cd backend && pytest tests/test_transfer_executor.py tests/test_worker.py tests/test_staging.py -q`; confirmar fallos relacionados.
- [x] Implementar exclusión en el loop antes de descarga, sin retirar entradas del manifiesto o debilitar la comparación de identidades MEGA. Si desaparece/reemplaza el objeto, sigue `MEGA_MANIFEST_REBUILD_REQUIRED`.
- [x] Verificar que el worker libera su reserva solo después del cierre/limpieza y que los fallos de limpieza detienen la ejecución y mantienen la acción de recuperación inhabilitada hasta su resolución. Probar publicación terminal antes de cleanup y lease vivo, y fallos al limpiar staging/MEGAcmd.
- [x] Pasar pruebas enfocadas y crear commit `feat: preserve omissions across transfer execution`.

## Task 4: Dependencias con aceptación explícita

**Files:** scheduling, stores, recovery, `main.py`, consumidores de evaluación; `test_transfer_scheduling.py`, `test_transfer_evaluation_flow.py`, pruebas API/store de recuperación.

**Interfaces:** ampliar `dependency_wait_reason(parent_status: str | None, *, parent_omitted_count: int = 0, parent_attempt_count: int = 1, accepted_parent_omissions_attempt: int | None = None) -> str | None`. Nuevo `AcceptParentOmissionsRequest(expected_parent_attempt: int, request_id: UUID)` y store `aaccept_parent_omissions(child_id: UUID, *, actor_id: UUID, expected_parent_attempt: int, request_id: UUID) -> TransferRecord`.

- [x] Escribir pruebas fallidas: padre con omisiones explícitas y advertencias bloquea hijo con `BLOCKED_PARENT_OMISSIONS`; padre con solo renombrados mantiene comportamiento anterior; padre `FAILED`/`CANCELLED` nunca se libera aceptando omisiones.
- [x] Ejecutar `cd backend && pytest tests/test_transfer_scheduling.py tests/test_transfer_evaluation_flow.py tests/test_transfer_recovery_api.py -q`.
- [x] Implementar el guard en evaluación, reserva del dispatcher y preflight final; contar omisiones de los padres en lote. El endpoint registra `TRANSFER_PARENT_OMISSIONS_ACCEPTED` y aceptación del intento vigente, comprobando que el hijo sigue pendiente sin reserva y que el padre es visible/terminal. Cambiar dependencia borra aceptación.
- [x] Probar aceptación idempotente y concurrencia con reanudación del padre/despacho del hijo en PostgreSQL: un intento nuevo exige nueva aceptación; revalidación bajo bloqueo antes de iniciar. La evaluación aprobada previamente no evita el guard final.
- [x] Pasar pruebas enfocadas y crear commit `feat: guard dependent transfers after source omissions`.

## Task 5: Reintento selectivo, reporte y avisos durables

**Files:** recovery, stores, `main.py`, nuevo report, notifications, worker; pruebas de recovery/report/stream/SMTP/worker.

**Interfaces:** `RetryOmittedRequest(expected_attempt: int, request_id: UUID, item_ids: list[UUID])`, 1..100 IDs únicos. Store `aretry_omitted_transfer_items(transfer_id: UUID, *, item_ids: list[UUID], actor_id: UUID, expected_attempt: int, request_id: UUID) -> TransferRecord`. `iter_omissions_csv(store: Any, transfer_id: UUID) -> AsyncIterator[str]`, bajo autorización terminal previa.

- [x] Escribir pruebas fallidas: solo IDs omitidos seleccionados vuelven a `PENDING`, se limpian metadatos actuales de omisión pero se conserva auditoría; decrementan resueltos/avisos, preservan bytes y rutas, política `STOP`, nuevo intento una vez. Rechazar estados activos, reservas, IDs ajenos/duplicados, cambios de credenciales y `SKIPPED` históricos.
- [x] Ejecutar `cd backend && pytest tests/test_transfer_recovery_api.py tests/test_transfer_recovery_store.py tests/test_transfer_omission_report.py -q`.
- [x] Implementar `retry-omitted` con las mismas garantías transaccionales y de autorización que tarea 2; no habilitar reanudación general indiscriminada de `COMPLETED_WITH_WARNINGS`. Mantener referencias de auditoría a la omisión anterior.
- [x] Implementar informe CSV terminal con cursor, escape CSV y neutralización de fórmulas `=`, `+`, `-`, `@`, tab/CR. Probar Unicode, comillas, saltos, manifiesto multipágina y acceso denegado.
- [x] Añadir `omitted`/`omitted_bytes` a REST, snapshot SSE inicial y frames posteriores. Guardar conteos terminales en el snapshot del outbox; adaptar worker y renderer a snapshots nuevos/antiguos. Mostrar conteos y enlace al historial, sin rutas completas en el correo.
- [x] Probar outbox del intento previo tras reintentar omitidos: cuerpo/conteos permanecen los de su snapshot. Pasar `test_transfer_stream.py`, `test_smtp_delivery.py`, `test_audit.py` y `test_worker.py` junto a pruebas nuevas. Commit `feat: report and selectively retry omitted files`.

## Task 6: Interfaz para recuperación y advertencias

**Files:** `transferTypes.ts`, `TransferFileReport.tsx`, pruebas del componente, `App.tsx`, `App.test.tsx`; CSS solo si los componentes existentes no cubren el ajuste.

**Interfaces:** `TransferFileReport` recibe `transfer`, páginas del manifiesto y callbacks `onSkipAndResume`, `onRetryOmitted`, `onDownloadOmissions`; tipos compartidos corresponden a DTOs de las tareas 1–5. Conservar fetch autenticado/CSRF y manejo de errores mediante `api.ts`.

- [x] Escribir pruebas fallidas de extracción del informe existente sin perder paginación y rutas asignadas. Botón «Omitir este archivo y continuar» solo cuando `can_skip`; confirmación con el texto exacto del diseño, deshabilitación durante envío y refresco ante `409`.
- [x] Ejecutar `cd frontend && npm test -- --run src/TransferFileReport.test.tsx src/App.test.tsx`.
- [x] Implementar componente e integración, contador de omitidos en activo/historial, advertencia persistente y fin de procesamiento distinto de cobertura de copia. El detalle conserva motivo/fecha; descarga CSV y selección 1..100 para reintentar omitidos.
- [x] Añadir aviso y acción de aceptación de padre con omisiones, mostrando el intento que se acepta; llamada obsoleta refresca el estado sin despachar el hijo desde el navegador.
- [x] Probar cero bytes, pestaña recargada, frames atrasados, error de red posterior al commit y reintento de la misma acción con el mismo `request_id`. Mantener la conducta existente de SSE/polling terminal y los controles de acceso del backend.
- [x] Crear commit `feat: surface file omission recovery and warnings` (`abfb928`).

## Task 7: Omisión automática opt-in de restricciones MEGA confirmadas

**Files:** runner MEGAcmd, nuevo módulo `backend/app/services/megacmd_failures.py`, omission policy, executor, `main.py`, stores, formulario en `App.tsx`; pruebas runner/provider/executor/API/UI.

**Interfaces:** `MegaFailureCategory = RESOURCE_RESTRICTED | ACCOUNT_RESTRICTED | AUTHENTICATION | QUOTA | LOCAL_IO | TRANSIENT | UNKNOWN`; `MegaFailureScope = FILE | ACCOUNT | UNKNOWN`; `classify_megacmd_failure(return_code: int, output: bytes) -> MegaFailureDiagnostic`, dataclass inmutable con `category`, `scope`, `retryable`, `safe_code`. La excepción guarda solo este diagnóstico seguro; no conserva salida cruda.

- [x] Contrastar documentación oficial de MEGAcmd/SDK. No se confirmó en la instalación una versión ni un mapeo del código de salida CLI a una restricción de archivo; no se inspeccionaron credenciales ni transferencias reales.
- [x] Probar código positivo 16 aislado, marcadores SDK, cuenta bloqueada, cuota, autenticación, transitorio, error local y salida desconocida: todos quedan `UNKNOWN`, sin retener salida/rutas. La puerta de política también exige MEGA, etapa `SOURCE_DOWNLOAD`, escritura no iniciada y limpieza exitosa.
- [x] Ejecutar suite backend completa, `ruff`, `mypy` y pruebas PostgreSQL desechables; todas pasan según la evidencia de tarea 8.
- [x] Implementar clasificación segura en memoria y predicados de seguridad, manteniendo deliberadamente todo diagnóstico actual como `UNKNOWN`. Crear y reanudar rechazan el opt-in automático; no se persiste una omisión automática ni se activa `CopyOutcome.SKIPPED` por clasificador.
- [x] Conservar por separado el circuito actual de fallos seguros; ningún error MEGAcmd desconocido altera la cuenta ni omite archivos.
- [ ] Habilitar la política automática, UI de opt-in y persistencia `MEGA_FILE_RESTRICTED` solo cuando se compruebe una firma individual de la versión instalada y se agreguen pruebas de esa versión. La entrega manual A permanece utilizable.
- [x] Crear commit `feat: add conservative MEGA failure diagnostics` (`7682f72`); la activación automática sigue pendiente de confirmar una firma individual de la versión instalada.

## Task 8: Verificación final, documentación y preparación de despliegue

**Files:** `docs/specs/04-transfers-engine-spec.md`, `05-realtime-telemetry-spec.md`, `06-audit-notifications-spec.md`, `docs/api/openapi-contracts.md`, `docs/integrations/transfer-scheduling.md`, `docs/implementation-status.md`, este plan.

- [x] Actualizar especificaciones/contratos y guía operativa con semántica de `SKIPPED`, defaults, endpoints, cortacircuitos y aceptación de dependencias. Registrar evidencia real separada de validación simulada.
- [x] Ejecutar backend completo: `pytest -q` (565 pasaron, 11 omitidas), `ruff check .` y `mypy app`. Las tres carreras de recuperación pasaron en PostgreSQL 16 desechable; `alembic upgrade head` también pasó. El downgrade ahora se protege si hay omisiones explícitas.
- [x] Ejecutar frontend completo: `npm test -- --run` (63 pasaron), `npm run lint` y `npm run build`. El recorrido de omisión y avisos se verificó con datos simulados.
- [x] Auto-revisar el diff contra los nueve criterios del diseño y los cinco focos de revisión; verificar que no se alteró la lista blanca de comandos ni se incluyeron secretos. Las revisiones resolvieron los seis hallazgos; no quedan hallazgos abiertos.
- [ ] Preparar despliegue coordinado para revisión: respaldo verificado, detener nuevas admisiones, drenar copias, aplicar migración, actualizar API/workers/Beat coherentemente y comprobar salud; después habilitar admisiones. El downgrade se bloquea mientras existan omisiones explícitas. Mantener esquema y retener/atender trabajos afectados antes de volver a un ejecutor antiguo.
- [ ] Presentar diff y evidencia al usuario. Solo tras autorización de despliegue ejecutar el procedimiento. Recuperar la transferencia real mediante la acción de UI/API con comprobación fresca de intento, fila y reservas; nunca mediante UPDATE manual. La acción concreta de omitir/reanudar se revisa con el usuario antes de iniciarla.
- [x] Commit de documentación `docs: document transfer omission operations and validation` (`5e57fbc`).

## Revisión del plan realizada

- Cobertura: manual → tareas 1–3 y 6; dependencias → 4; informe/correo/reintento → 5–6; automática → 7; verificación/despliegue → 8.
- Las acciones nuevas usan UUID de manifiesto y condición de intento; el ID público solo identifica el trabajo para el usuario.
- Separación explícita entre `skipped` histórico, `omitted` nuevo y `failed`; ningún conteo presume copia exitosa.
- La compatibilidad histórica rechaza inferencias ambiguas y no exige cambiar el manifiesto real para hacer pruebas.
- La entrega automática tiene criterio de activación verificable; la omisión manual no depende de resolverlo.
