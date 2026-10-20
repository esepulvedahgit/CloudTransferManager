# Scheduled Transfer Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task in this session. Steps use checkbox (`- [ ]`) syntax for tracking. Do not delegate without authorization.

**Goal:** Evaluar las selecciones al programarlas y mostrar su conteo parcial o completo en Programaciones, bloqueando la ejecución cuando excedan los límites.

**Architecture:** Persistir la evaluación separada del estado de copia y procesarla en una cola con worker propio. Compartir la enumeración del origen con el ejecutor actual; el scheduler solo admite evaluaciones aprobadas. Publicar el conteo en las respuestas existentes y actualizar cada fila de Programaciones por SSE y polling.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL 16, Celery/Redis, React, TypeScript, MUI, TanStack Query, pytest y Vitest.

**Spec:** [Diseño de evaluación anticipada](../specs/2026-10-17-scheduled-transfer-evaluation-design.md).

## Global Constraints

- Solo COPY; evaluar únicamente metadatos, sin descargas ni escrituras remotas.
- TRANSFER_MAX_FILES=5000 por defecto; SMB_TRANSFER_MAX_FILES=100000 para origen SMB.
- No introducir comandos Rclone fuera de lsjson, copy, about, size; sin shell=True.
- Mantener UUID v4, códigos TRF-YYYY-NNNNNN, cifrado y exclusión de secretos de API/logs.
- Límite+1 basta para bloquear; count_complete=false indica conteo parcial.
- Una actualización de conteo por segundo como máximo; heartbeat de cinco segundos.
- Reserva recuperable tras 60 segundos sin heartbeat; timeout de intento de 600 segundos.
- Tres intentos por generación; esperas de 60 y 120 segundos antes de ERROR.
- La evaluación no consume staging ni cupos de copias; worker evaluations con concurrencia uno.
- Nueva evaluación obligatoria para solicitudes programadas; tareas inmediatas no evaluadas mantienen NOT_REQUIRED.
- No reintentar trabajos terminales ni desplegar automáticamente al completar el plan.
- Preservar todos los cambios locales preexistentes; no incluirlos en commits de esta mejora.

## Review Focus

1. Límite exacto, selección solapada y nombres iguales: contar identidades únicas, no carpetas ni rutas seleccionadas (tarea 2).
2. Caída tras reservar/publicar y actualización de un intento vencido: una evaluación vigente y ningún resultado antiguo aceptado (tarea 3).
3. Cancelación o cambio de dependencia durante un recorrido: invalidar el resultado y mantener la tarea sin ejecutar (tareas 3 y 5).
4. EXCLUSIVE bloqueada por evaluación y padre que todavía crea el origen: no detener la cola ni rechazar anticipadamente al hijo (tarea 4).
5. Recarga de página o SSE fuera de orden: mostrar el último conteo persistido, diferenciar desconocido/cero y reinicios de generación (tarea 6).

---

## Preparación y mapa de archivos

Leer AGENTS.md, docs/implementation-status.md, specs/02, 04, 05 y 07, docs/api/openapi-contracts.md y docs/integrations/transfer-scheduling.md antes de ejecutar. Usar las rutas de docs/ relativas al repositorio para esas especificaciones. El árbol ya contiene modificaciones de fiabilidad y proveedores; identificarlas mediante diff antes de editar. Crear aislamiento al ejecutar si puede conservar esas dependencias sin perder cambios locales.

| Unidad | Archivos |
| --- | --- |
| Persistencia y DTO | backend/app/models.py; backend/app/services/transfer_evaluation.py (nuevo); backend/app/services/database_store.py; backend/app/services/memory_store.py; backend/migrations/versions/20261017_0013_transfer_evaluations.py (nuevo) |
| Inventario compartido | backend/app/services/source_inventory.py (nuevo); backend/app/services/transfer_executor.py |
| Evaluación y Celery | backend/app/services/transfer_evaluator.py (nuevo); backend/app/worker.py; backend/app/settings.py |
| API y scheduler | backend/app/main.py; backend/app/services/transfer_scheduling.py; stores anteriores |
| Presentación | frontend/src/ScheduledTransferEvaluation.tsx (nuevo); frontend/src/App.tsx; frontend/src/api.ts si requiere exportar el tipo |
| Infraestructura | compose.yaml; .env.example; backend/tests/test_health.py; scripts/check_compose_network.py |
| Documentación | docs/integrations/transfer-scheduling.md; docs/api/openapi-contracts.md; docs/specs/04-transfers-engine-spec.md; docs/specs/05-realtime-telemetry-spec.md; docs/specs/07-infrastructure-docker-spec.md; docs/implementation-status.md |

### Task 1: Modelo durable y contrato de evaluación

**Files:** modelos, migración, transfer_evaluation.py y ambos stores del mapa. **Tests:** backend/tests/test_transfer_evaluation_store.py (nuevo), backend/tests/test_database_store.py.

**Interfaces:** `EvaluationRecord` en transfer_evaluation.py contiene los campos de la especificación; `public_dict() -> dict[str, Any]` expone solo los campos públicos. `TransferRecord.evaluation: EvaluationRecord | None` se serializa como un objeto con status NOT_REQUIRED si falta. `alist_transfer_evaluations(transfer_ids: list[UUID]) -> dict[UUID, EvaluationRecord]` hidrata páginas en lote.

- [x] Escribir pruebas de creación programada atómica, inmediata NOT_REQUIRED, replay idempotente y serialización sin tokens/credenciales; añadir prueba de una página sin consultas por elemento.
- [x] Ejecutar desde backend: `pytest tests/test_transfer_evaluation_store.py -v --tb=short`; comprobar fallo por la interfaz nueva ausente.
- [x] Implementar tabla, DTO e integración en lectura/escritura de ambos stores. Usar comprobaciones de estado y cantidades no negativas en base de datos.
- [x] Implementar migración 0013 sobre 0012: backfill únicamente de programadas pendientes sin reserva de copia ni ejecución iniciada; comprobar upgrade y downgrade en base desechable, sin acceder a producción.
- [x] Ejecutar `pytest tests/test_transfer_evaluation_store.py tests/test_database_store.py -v --tb=short`; exigir resultado verde antes de avanzar.
- [ ] Revisar diff y preparar commit solo con los fragmentos propios: `feat(scheduling): persist early evaluation state`.

### Task 2: Extraer la enumeración y emitir conteos parciales

**Files:** source_inventory.py, transfer_executor.py. **Tests:** backend/tests/test_source_inventory.py (nuevo), backend/tests/test_transfer_executor.py, backend/tests/test_smb_manifest.py.

**Interfaces:** `SourceInventory` contiene `files: list[tuple[StorageItem, str]]` y `directories: list[tuple[StorageItem, str]]`. `InventoryProgress` contiene `files_found: int` y `count_complete: bool`. `SourceInventoryScanner.scan(source: StorageProvider, record: TransferRecord, *, allow_unknown_size: bool, maximum_files: int, on_progress: Callable[[InventoryProgress], Awaitable[None]], should_cancel: Callable[[], Awaitable[bool]]) -> SourceInventory` conserva las reglas de tamaño mediante Settings recibidos en su constructor. `InventoryPreflightError` transporta error_code, message, technical_details y el último InventoryProgress.

- [x] Escribir pruebas: 5000 archivos aprueban; 5001 bloquean con count_complete=false; las carpetas no cuentan; selecciones solapadas no duplican; archivos homónimos con distintos object_id sí cuentan; cursores repetidos fallan; inventario vacío termina en cero completo.
- [x] Añadir pruebas de cancelación, límites de tamaño y progreso parcial anterior al fin usando páginas simuladas, sin consultar servicios reales.
- [x] Ejecutar `pytest tests/test_source_inventory.py -v --tb=short` y comprobar el fallo esperado.
- [x] Extraer la enumeración actual y adaptar el ejecutor a consumirla conservando estructura, identidad y errores. El callback decide la frecuencia de persistencia; el scanner informa cantidades sin escribir manifiestos ni destinos.
- [x] Ejecutar `pytest tests/test_source_inventory.py tests/test_transfer_executor.py tests/test_smb_manifest.py -v --tb=short` y comprobar que no cambian las asignaciones de destino existentes.
- [ ] Revisar el diff frente a los cambios locales previos y preparar commit: `refactor(transfers): share read-only source inventory`.

### Task 3: Servicio de evaluación con reservas, recuperación y cola independiente

**Files:** transfer_evaluator.py, transfer_evaluation.py, ambos stores, worker.py, settings.py. **Tests:** backend/tests/test_transfer_evaluator.py (nuevo), backend/tests/test_transfer_evaluation_store.py, backend/tests/test_worker.py.

**Interfaces:** `EvaluationClaim` incluye transfer_id, generation y claim_token. Stores: `aclaim_due_evaluations(now: datetime, *, limit: int = 1) -> list[EvaluationClaim]`, `aheartbeat_evaluation(claim: EvaluationClaim, now: datetime) -> bool`, `asave_evaluation_progress(claim: EvaluationClaim, progress: InventoryProgress, now: datetime) -> bool`, `afinish_evaluation(claim: EvaluationClaim, *, status: str, progress: InventoryProgress, error_code: str | None, message: str | None, now: datetime) -> bool`. Servicio: `TransferEvaluator.execute(claim: EvaluationClaim) -> None`; constructor recibe settings, store y publicador. Celery: `dispatch_pending_evaluations()` en control y `run_transfer_evaluation(transfer_id: str, generation: int, claim_token: str)` en evaluations.

- [x] Escribir pruebas del recorrido aprobado/bloqueado, progreso persistido cada segundo como máximo, heartbeat independiente del conteo, timeout y tres intentos con esperas 60/120; al agotarse, ERROR sin estado FAILED de copia.
- [x] Añadir pruebas con PostgreSQL para dos reclamaciones simultáneas, redelivery, reserva vencida y proceso antiguo todavía vivo; actualización con token/generación antiguos debe devolver false.
- [x] Añadir pruebas de cancelación durante listado, limpieza de sesión MEGA y caída de Redis después de guardar el resultado; el polling debe recuperar el estado durable.
- [x] Ejecutar las pruebas nuevas para confirmar fallos y luego implementar reservas y servicio. Reusar los factories y contextos de proveedores del ejecutor; extraer solo el código compartido necesario. No instanciar un ejecutor para llamar a métodos privados.
- [x] Registrar despacho Beat cada 15 segundos, rutas de colas y recuperación. Mantener el trabajo de control limitado a operaciones de base de datos y publicación.
- [x] Ejecutar `pytest tests/test_transfer_evaluator.py tests/test_transfer_evaluation_store.py tests/test_worker.py -v --tb=short`; ejecutar también los casos de concurrencia contra PostgreSQL desechable.
- [ ] Preparar commit: `feat(scheduling): evaluate scheduled selections in background`.

### Task 4: Condicionar la admisión y preservar dependencias y exclusividad

**Files:** database_store.py, memory_store.py, transfer_scheduling.py. **Tests:** backend/tests/test_transfer_scheduling.py.

**Interfaces:** `evaluation_wait_reason(evaluation: EvaluationRecord | None) -> str | None` devuelve WAITING_EVALUATION para PENDING/EVALUATING, BLOCKED_EVALUATION para BLOCKED/ERROR y WAITING_PARENT para WAITING_PARENT; APPROVED/NOT_REQUIRED no agregan espera. El dispatcher combina esta regla con autorización, versiones y límites vigentes antes de reservar copias.

- [x] Escribir pruebas: hora vencida con conteo en curso no despacha; APPROVED sí puede despachar; NOT_REQUIRED conserva tareas inmediatas/históricas admitidas; una EXCLUSIVE sin aprobación no impide admitir trabajos posteriores compatibles.
- [x] Añadir pruebas de padre RUNNING sin consultas remotas, padre exitoso que habilita evaluación, padre FAILED que bloquea, cambio del límite que invalida aprobación y origen modificado que falla en la validación final existente.
- [x] Ejecutar `pytest tests/test_transfer_scheduling.py -v --tb=short` y confirmar los fallos de las nuevas reglas.
- [x] Implementar las condiciones de admisión en ambos stores. Resolver dependencia y accesos antes de reclamar evaluación; no adoptar silenciosamente credenciales nuevas. Conservar la validación al ejecutar y mantener el horario solicitado si vence durante la revisión.
- [x] Ejecutar `pytest tests/test_transfer_scheduling.py tests/test_worker.py -v --tb=short`.
- [ ] Preparar commit: `feat(scheduling): gate dispatch on evaluation approval`.

### Task 5: Exponer evaluación, reevaluación y eventos ordenados

**Files:** main.py, worker.py, event_bus.py solo si cambia el contrato de publicación; stores. **Tests:** backend/tests/test_transfer_evaluation_api.py (nuevo), backend/tests/test_transfer_stream.py.

**Interfaces:** las respuestas existentes incorporan `evaluation` conforme al DTO de tarea 1. `POST /api/v1/transfers/{transfer_id}/evaluation` devuelve 202 y la evaluación vigente; propietario/ADMIN y CSRF obligatorios. `arequest_transfer_evaluation(transfer_id: UUID, now: datetime) -> EvaluationRecord` reinicia solo si no hay intento pendiente/en curso y la tarea no fue admitida; invalida resultados previos aumentando generation y reiniciando sequence. Los conflictos usan el patrón de excepciones/HTTP actual.

- [x] Escribir pruebas de acceso ajeno, ADMIN, ausencia de CSRF, 409 para tareas admitidas/terminales y repetición de solicitudes sin duplicar evaluaciones.
- [x] Probar creación sin llamadas remotas, conversión a programada, cambio de dependencia que invalida una evaluación en curso y cambio solo de fecha que conserva la aprobación. Verificar que quitar la fecha no permite eludir evaluación.
- [x] Probar GET/listado/SSE con los mismos campos públicos y count_complete; eventos de evaluación no alteran bytes de copia ni attempt_count de ejecución.
- [x] Ejecutar `pytest tests/test_transfer_evaluation_api.py tests/test_transfer_stream.py -v --tb=short`; implementar DTO, endpoint y eventos hasta obtener verde.
- [x] Añadir límites efectivos cloud/SMB a GET /api/v1/transfers/capabilities para explicar la restricción en el formulario antes de guardar.
- [ ] Preparar commit: `feat(api): expose scheduled evaluation progress and retries`.

### Task 6: Conteo visible en cada fila de Programaciones

**Files:** ScheduledTransferEvaluation.tsx, App.tsx y tipos API según el mapa. **Tests:** frontend/src/ScheduledTransferEvaluation.test.tsx (nuevo), frontend/src/App.test.tsx.

**Interfaces:** `TransferEvaluation` refleja únicamente el DTO público. `ScheduledTransferEvaluation({ evaluation, onRetry, retrying }: { evaluation: TransferEvaluation; onRetry: () => void; retrying: boolean })` muestra textos de la especificación. Extender `mergeTransferState(current: Transfer, incoming: Transfer): Transfer` para ordenar la evaluación por generation y sequence de forma independiente de las métricas de ejecución.

- [x] Escribir pruebas de PENDING sin cero engañoso; EVALUATING con 3200 encontrados; APPROVED con 4180 completos; BLOCKED con al menos 5001/límite5000; vacío completo; ERROR y WAITING_PARENT.
- [x] Añadir pruebas de fila bloqueada que permanece en Programaciones, cancelación, Volver a evaluar y aviso a los 60 segundos mediante temporizadores simulados. Mostrar fecha de la evaluación y límite proporcionado por API.
- [x] Añadir pruebas de recarga desde datos persistidos, ausencia de SSE con polling, eventos fuera de orden y generación nueva cuyo conteo baja legítimamente a cero. No permitir que un snapshot atrasado oculte una evaluación más reciente.
- [x] Ejecutar desde frontend: `npm test -- src/ScheduledTransferEvaluation.test.tsx src/App.test.tsx`; confirmar primero los fallos de la funcionalidad nueva.
- [x] Implementar el componente y conectarlo a la fila existente, invalidación de queries y eventos. Cambiar el aviso de guardado a «Solicitud guardada; evaluando selección». Mantener los controles accesibles en pantallas estrechas.
- [x] Ejecutar `npm test -- src/ScheduledTransferEvaluation.test.tsx src/App.test.tsx`, `npm run lint` y `npm run build`.
- [ ] Preparar commit: `feat(ui): show live file counts in scheduled transfers`.

### Task 7: Worker de evaluación y documentación operativa

**Files:** compose.yaml, .env.example, tests de worker/salud, script de red y documentos del mapa.

**Interfaces:** servicio evaluation-worker con cola evaluations, concurrencia uno y sin puertos publicados; configuración de heartbeat/timeout/reintentos mediante constantes con los valores de la especificación. Exponer disponibilidad del consumidor de evaluación en la salud existente, indicando explícitamente si falta capacidad para evaluar sin declarar caída una copia que está ejecutándose.

- [x] Añadir pruebas de routing y consumidor, secretos declarados, staging/configuración efímera requerida por los adaptadores, límites CPU/memoria/PIDs y ausencia de puertos externos. Leer docs/specs/07 antes de modificar Compose.
- [x] Añadir el servicio con la imagen backend, acceso a PostgreSQL/Redis y salida a proveedores; no dar secretos de evaluación al control-worker.
- [x] Documentar campos, estados, endpoint, tiempos orientativos, conteos parciales, dependencia, reevaluación y comportamiento al vencer el horario. Actualizar implementation-status con evidencia real, diferenciando implementación de despliegue.
- [x] Ejecutar `pytest tests/test_worker.py tests/test_health.py -v --tb=short` desde backend y `python scripts/check_compose_network.py` desde la raíz; revisar `docker compose config --quiet` sin imprimir secretos.
- [ ] Preparar commit: `ops(scheduling): provision evaluation worker and document behavior`.

### Task 8: Verificación integrada y preparación de entrega

**Files:** backend/tests/test_transfer_evaluation_flow.py (nuevo); especificaciones y guía operativa si aparecen discrepancias.

- [x] Crear prueba de flujo con proveedores simulados: programar un árbol de 5001 archivos, observar progreso parcial, cerrar/reabrir la consulta, terminar bloqueado y adelantar el reloj para comprobar que no hay copia ni reserva de staging.
- [x] Crear flujo de 5000 archivos: aprobar, respetar horario/dependencia y llegar al ejecutor; cambiar el árbol antes de ejecutar para confirmar que la validación final sigue vigente.
- [x] Crear escenario con copia larga simulada y evaluación independiente: el conteo debe avanzar sin esperar a liberar el proceso transfers. Verificar que un fallo del evaluation-worker muestra espera y no bloquea otras copias admisibles.
- [x] Ejecutar suite backend `pytest -v --tb=short`, `ruff check .` y `mypy app`; frontend `npm test`, `npm run lint` y `npm run build`. Corregir regresiones antes de avanzar. Ejecutar garantías de concurrencia también en PostgreSQL, sin usar la base activa.
- [x] Ensayar migración/backfill con una copia desechable de esquemas/datos sintéticos que incluya tareas futuras, bloqueadas, admitidas y terminales; no reiniciar servicios reales como parte de las pruebas.
- [x] Revisar diff final frente al estado inicial y dejar listado de archivos, resultados de pruebas y pasos concretos de despliegue. Recomendar ventana sin tareas admitidas y arranque coordinado de API/control/evaluation-worker. Conservar las tablas/columnas nuevas si se revierte la aplicación; no ejecutar downgrade con evaluaciones en uso.

## Criterios de entrega

- El conteo se ve directamente en Programaciones, durante y después de la evaluación, y sobrevive a recarga/reinicio.
- Una selección superior al límite se bloquea antes de la hora y conserva un motivo accionable.
- El paso del tiempo no aprueba ni rechaza por sí mismo la selección; la demora se comunica.
- Copias, staging, dependencias, idempotencia, cancelación y nombres conservan sus garantías.
- La entrega declara exactamente qué se probó y qué falta validar con proveedores de la instalación.

## Revisión del plan

El plan cubre las secciones del diseño y asigna los cinco riesgos de Review Focus a pruebas específicas. La ejecución recomendada es en esta misma sesión, por tareas, porque modelo, scheduler y UI comparten el contrato de evaluación y conviene revisar cada transición antes de continuar. La implementación, las pruebas y los límites de despliegue se acreditan en el ledger de progreso y el estado de implementación.

Ejecución registrada el 2026-10-17: las tareas funcionales 1–8 quedaron implementadas y verificadas según el [ledger de progreso](../../../.superpowers/sdd/2026-10-17-scheduled-transfer-evaluation/progress.md). Los pasos de commit de cada tarea siguen pendientes para no incluir cambios locales preexistentes; la migración `20261017_0013` y el despliegue también siguen pendientes.
