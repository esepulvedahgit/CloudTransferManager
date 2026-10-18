# Estado de implementación

**Revisión del repositorio: 2026-10-18.** Este documento resume lo que existe en el código y separa esa evidencia de la validación que depende de CI, credenciales o redes de una instalación.

## Estado por área

| Área | Estado en el repositorio | Evidencia y límites |
|---|---|---|
| Autenticación y usuarios | Implementado | Sesiones HttpOnly/SameSite con CSRF, Argon2id, bloqueo tras cinco intentos fallidos, roles `ADMIN`/`USER`, cambio obligatorio de contraseña y CLI inicial. Véanse `backend/app/main.py`, `backend/app/security/` y `backend/tests/test_api_security.py`, `test_user_management.py`, `test_cli.py`. |
| Conexiones y credenciales | Implementado | Administración, asignación privada, OAuth de Google, cifrado AES-GCM y respuestas públicas sin credenciales. El archivado es lógico y rechaza conexiones con trabajos activos. Véanse `backend/app/main.py`, `backend/app/security/credentials.py` y las migraciones. |
| Proveedores | Implementado con validación externa | MEGA usa MEGAcmd; Google Drive usa Rclone/OAuth; Seafile ofrece adaptadores Rclone o API explícito; la UI usa SMB/NAS como origen; Nextcloud usa Rclone WebDAV con URL y credenciales administradas por conexión. Cada instalación necesita sus credenciales, permisos y rutas de red. La compatibilidad de Seafile API se acota inicialmente a CE 13.x. Nextcloud requiere prueba real por instalación antes de activarlo. |
| Explorador | Implementado | Navegación, búsqueda, selección múltiple y destino por carpeta existente; paneles coordinados en escritorio y pasos separados en pantallas estrechas. No hay creación manual de carpetas desde la UI. Véanse `frontend/src/App.tsx` y `docs/specs/03-file-explorer-spec.md`. |
| Transferencias | Desplegado en Compose; validación con proveedores reales pendiente | Las transferencias nuevas usan `KEEP_BOTH` global, inventario por identidad, asignación de nombres `_01`, `_02`, manifiesto común y staging por archivo. La recuperación manual puede omitir explícitamente un fallo elegible, conserva las copias verificadas y continúa con los restantes; el CSV, el reintento selectivo, la aceptación por intento de omisiones del padre y el panel paginado de fallos/omisiones están disponibles. `SKIPPED` histórico se distingue de `omitted`. La pausa durable en límites entre archivos está desplegada para trabajos compatibles; las ejecuciones heredadas activas sin límite seguro informan que no admiten pausa. La política automática MEGA sigue deshabilitada: no se verificó una firma por archivo ligada a la versión instalada, así que las fallas quedan `UNKNOWN`. Rclone no reemplaza objetos existentes; Seafile API solicita `replace=false`; los resultados de escritura ambiguos solo se aceptan tras reconciliar SHA-256. Migraciones `20261018_0016` y `20261018_0017` aplicadas en la instalación. |
| Programaciones | Desplegado en Compose; prueba de flujo real pendiente | Ejecuciones futuras de una sola vez, zonas IANA, modos `AUTO`/`EXCLUSIVE`, dependencias, despacho durable vía PostgreSQL/Celery Beat/`control-worker` y estados `PAUSING`/`PAUSED`. Las tareas programadas muestran conteo parcial/completo y motivo de espera en Programaciones; se conservan horario e intento al pausar y reanudar. Evaluaciones bloqueadas no reservan copias y el dispatcher mantiene el preflight final. Compose incluye `evaluation-worker` aislado y `/health/evaluations` separado de readiness. Detalle en `docs/integrations/transfer-scheduling.md`. |
| Progreso e historial | Desplegado en Compose | SSE sobre Redis, reconciliación por `attempt_count`/`state_version` entre SSE, polling e historial, paginación SQL de incidentes y resultados por archivo para manifiestos SMB. Los trabajos `PAUSED` cierran su stream hasta reanudarse. Véanse `docs/specs/05-realtime-telemetry-spec.md` y el contrato API. |
| Auditoría y SMTP | Implementado con límite por confirmar | Registro de eventos, sanitización, configuración/prueba SMTP desde Administración, secretos SMTP cifrados y outbox transaccional por intento terminal. La creación de transferencias y su evento de auditoría comparten transacción; el envío usa worker/cola aislados, tokens de despacho y de intento, reintentos durables y vista ADMIN de estados sin correo del destinatario. `/health/notifications` vigila el worker sin degradar readiness de copias. SMTP no garantiza exactamente una entrega si el proceso cae tras la aceptación remota. La aplicación escribe auditoría append-only, pero su inmutabilidad no está impuesta por trigger/permisos de PostgreSQL. |
| Despliegue y CI | Compose actualizado el 2026-10-18; CI independiente | Backend, frontend y workers se reconstruyeron desde `develop`; PostgreSQL aplicó `20261018_0016` y `20261018_0017`. `/health/ready`, `/health/evaluations`, `/health/notifications` y el proxy HTTPS respondieron correctamente. Compose mantiene PostgreSQL, Redis y workers en redes internas; solo el proxy publica puertos. La conectividad de proveedores y los recorridos de copia/pausa con una transferencia dedicada siguen pendientes. |

## Capacidades añadidas después del alcance inicial

- Migración de carpetas desde SMB/NAS a MEGA, Google Drive o Seafile, con manifiesto por archivo y reanudación controlada.
- Seafile mediante Rclone o API directa seleccionada por conexión; no hay fallback silencioso entre adaptadores.
- Nextcloud mediante WebDAV/Rclone con registro desde la UI y límites conservadores de concurrencia, ancho de banda y timeout. Véase `docs/integrations/nextcloud-compatibility.md`.
- Programación única, modos de admisión, dependencias y vista de trabajos en segundo plano con evaluación anticipada, conteo recuperable y bloqueo previo al despacho por límite.
- Administración de conexiones privadas y archivado lógico sin borrar referencias históricas.
- Configuración SMTP desde Administración y menú de cuenta que agrupa administración, cambio de contraseña y cierre de sesión.
- CI de backend/frontend y análisis Trivy de repositorio, configuración e imágenes.
- Conservación y numeración de nombres ocupados en las transferencias nuevas de todos los proveedores reales admitidos; el contenido igual también recibe un nombre numerado. Mock sigue simulando progreso sin modelar escrituras.
- Omisiones manuales auditadas de fallos MEGA elegibles, continuidad por manifiesto, informe CSV, reintento selectivo, avisos de progreso/correo y aceptación explícita de omisiones del intento padre. La omisión automática está protegida y rechazada hasta demostrar una firma individual de MEGAcmd para la versión instalada.

## Remediación de resiliencia (2026-10-14)

| Riesgos | Cambio aplicado | Estado / límite |
|---|---|---|
| R1–R2 | Las escrituras de transferencia rechazan intentos/estados atrasados; el login actualiza solo sus contadores y la protección del último ADMIN se serializa en PostgreSQL. | Tests unitarios y de concurrencia PostgreSQL. |
| R3, R5, R7 | SSE/eventos son best-effort respecto al resultado de copia; lectores de subprocesos y heartbeat se supervisan; el worker consulta cancelación durable en PostgreSQL. | No se ensayó una partición real de PostgreSQL durante una escritura en proveedor. |
| R4, R11 | Idempotencia de creación y outbox de correo con lease, retry, deduplicación local y worker dedicado. Los mensajes de correo antiguos se convierten de forma idempotente en filas del outbox antes de ser confirmados. | Migraciones 0010/0011 pendientes de aplicar; una caída tras aceptación SMTP puede duplicar correo. |
| R6 | Reconciliación SMB compara SHA-256 y no declara verificado un resultado remoto ambiguo sin evidencia suficiente. | Depende de que origen y destino permitan lectura/checksum; no se habilitó borrado ni reintento ciego. |
| R8–R9 | Ready verifica consumidores `transfers` y `control` y heartbeat de Beat; correo tiene salud separada. Compose limita CPU/memoria/PIDs, rota logs y configura Redis sin eviction. El historial transfiere páginas desde SQL. Un barrido cada 10 minutos recoge carpetas de staging antiguas si su transferencia no está activa. | No hay aún alertas externas ni detector de progreso por tarea. El barrido protege transferencias activas y homes `megacmd-*`; su ejecución puede esperar a que la cola de copias quede libre. |
| R10 | Integración opcional de snapshot PostgreSQL cifrado a MEGA S4; RPO/RTO objetivo: 15 minutos / 2 horas. | Suite backend y pruebas con S3 simulado aprobadas; dump/restore PostgreSQL y conciliación se probaron en una instancia desechable. Aún falta endpoint/bucket/credenciales y medir una restauración con datos de la instalación; no se aplicaron migraciones ni reiniciaron servicios activos. El RPO depende de que el dump y la carga completen dentro del umbral de 15 minutos. |
| R12 | El frontend combina SSE y polling sin permitir que una señal atrasada revierta un estado terminal. | Validado con pruebas de UI; falta recorrido visual/manual por viewport. |

Las guías específicas están enlazadas desde [Documentación relacionada](../README.md#documentación-relacionada). Las restricciones de despliegue y los directorios temporales se describen en el [README](../README.md#temporales-secretos-y-datos-persistentes).

## Criterios de aceptación originales

La siguiente agrupación ayuda a estimar el avance, pero separa implementación de evidencia reciente. La existencia de una prueba o workflow no significa que haya pasado en el commit actual.

| Criterios | Estado de implementación | Evidencia que aún depende de ejecución o entorno |
|---|---|---|
| 1–4: autenticación, alta de usuarios y roles | Implementados | Resultado actual de tests backend. |
| 5–8: MEGA, OAuth Google y cifrado | Implementados en código | Autorizar/probar los proveedores con credenciales reales y confirmar los secretos del despliegue. |
| 9: conectividad a MEGA y Google Drive | Endpoints y adaptadores implementados | Éxito real depende de credenciales, OAuth, permisos y red de la instalación. |
| 10–14: exploración y selección | Implementados | Validación visual del recorrido en los tamaños de pantalla objetivo. |
| 15–16: copia en ambos sentidos | Implementada con pruebas automatizadas de adaptadores/procesador | Copia real con cuentas de prueba no se verificó en esta revisión. |
| 17–22: SSE, continuidad, cancelación y errores seguros | Implementados | Resultado del pipeline actual y comprobación operativa del worker/Redis. |
| 23–24: notificaciones de correo | Implementadas y configurables | Entrega real requiere SMTP operativo y no se probó aquí; el estado del consumidor se expone aparte de la readiness de copias. |
| 25–27: historial, auditoría de solicitud y Compose | Implementados en código/configuración | Inicio completo del stack y revisión de respaldos no se ejecutaron aquí. |
| 28: interfaz responsive | Adaptaciones responsive presentes | Falta registrar una revisión visual/manual por viewport. |
| 29: suite automatizada aprobada | Tests y gates CI configurados | Debe verificarse el resultado para el commit candidato. |
| 30: documentación de instalación y operación | Documentada en README, specs y guías | Mantener las guías alineadas cuando cambien los contratos o la configuración. |

## Qué falta para declarar la entrega validada

La implementación cubre el flujo principal y varias extensiones que no estaban en el alcance inicial. No se puede afirmar un porcentaje exacto de aceptación: la lista de criterios aún no tiene evidencia de aprobación por criterio y el estado del pipeline cambia con cada commit.

Antes de declarar una versión lista para una instalación concreta, falta registrar evidencia de:

1. Todos los jobs de CI verdes para el commit candidato (`ruff`, tests y `mypy`; tests, lint y build del frontend; Trivy y builds Docker).
2. Conectividad y copia real con credenciales de Google Drive y MEGA; para extensiones habilitadas, validar además Seafile CE 13.x, Nextcloud y NAS desde las redes del backend/worker.
3. Revisión visual responsive en los tamaños soportados y recorrido de los flujos administrativos.
4. Decisión explícita sobre dos requisitos de hardening que siguen en la guía operativa: rate limiting de API e inmutabilidad de auditoría impuesta por la base de datos. No se encontraron implementaciones equivalentes en el árbol revisado.
5. Si SMB debe ser estrictamente solo origen, validar ese límite también en `POST /api/v1/transfers`: la UI filtra SMB del selector de destino, pero el endpoint no comprueba el tipo de proveedor destino.
6. Registrar la cobertura de los 30 criterios de aceptación como implementada, verificada en CI, validada contra proveedor real o pendiente, sin mezclar esas categorías.

La evidencia local de esta revisión no equivale a un resultado de CI sobre un commit ni valida una instalación concreta. El alcance de las pruebas y los límites de proveedor/despliegue quedan registrados abajo.

## Evidencia de validación previa (2026-10-17)

- Backend: `pytest -v --tb=short`: **486 pasaron, 9 omitidas**. Se ejecutaron además los archivos de persistencia y store con PostgreSQL 16 desechable: **41 pasaron**, incluidas las carreras de reserva/recovery. La base temporal no publicó puertos ni usó volúmenes persistentes.
- Calidad backend: `ruff check .` pasó; `mypy app` no encontró problemas en 44 archivos.
- Frontend: `npm test`: **53 pasaron**; `npm run lint` y `npm run build` pasaron. Vite reportó avisos de anotaciones de comentarios en dependencias `zod`, sin impedir el build.
- Compose: `docker compose config --quiet` y `scripts/check_compose_network.py` pasaron; el worker de evaluación no expone puertos y mantiene su cola separada.
- Alcance de la evidencia: se probaron proveedores simulados y SQLite/PostgreSQL desechables. No se aplicó la migración `20261017_0013` a la base de la instalación, no se recrearon servicios activos y no se hicieron copias con cuentas reales. La ejecución de la migración y el despliegue coordinado siguen pendientes.

## Evidencia de validación de omisiones previa al despliegue (2026-10-18)

- Backend completo: `pytest -q`: **565 pasaron, 11 omitidas**. `ruff check .` pasó y `mypy app` no encontró problemas en 48 archivos.
- PostgreSQL 16 desechable: **3 pasaron** para omitir/reanudar idempotente, carrera entre reintento del padre y despacho del hijo, y reintento concurrente de omisiones. También pasó `alembic upgrade head`, incluida `20261018_0015`. Las DB usaron redes Docker internas sin puertos publicados y se retiraron al terminar.
- Frontend completo: **63 pasaron**; `npm run lint` y `npm run build` pasaron. Rollup mostró dos avisos de anotaciones de comentarios en dependencias `zod`, sin impedir el build.
- Revisión de código: los hallazgos sobre fallos transitorios/de integridad, downgrade, conteos paginados, idempotencia entre acciones y replays ligados al actor quedaron corregidos; no hay hallazgos abiertos en la revisión final.
- `docker compose config --quiet` pasó usando `.env.example` solo para resolver la configuración local.
- Al cierre de esa validación aún no se habían aplicado migraciones ni reconstruido servicios. No se accedió al manifiesto real ni se actuó sobre `TRF-2026-000013`. No se verificó una versión concreta de MEGAcmd instalada en el servicio, así que la omisión automática permanece deshabilitada.

## Evidencia de incidentes y pausa entre archivos (2026-10-18)

- Backend completo: `pytest -q --tb=short`: **586 pasaron, 12 omitidas**. `ruff check app tests migrations` pasó y `mypy app` no encontró problemas en 48 archivos.
- Integración PostgreSQL 16 desechable: pasaron la carrera real entre solicitud de pausa y checkpoint de archivo, el rechazo de progreso con un `dispatch_token` reemplazado y la regresión de finalización terminal ante una cancelación concurrente. Upgrade `alembic upgrade head`, downgrade hasta `20261018_0015` y upgrade de nuevo pasaron; también se verificó que el downgrade de pausa se niega si hay estados `PAUSING`/`PAUSED`.
- Frontend: **66 pruebas pasaron**; `npm run lint` y `npm run build` pasaron. Vite conserva dos avisos no bloqueantes de anotaciones en dependencias `zod`.
- Revisión de código: sin hallazgos críticos, importantes ni menores abiertos después de ajustar fencing por despacho, precedencia de cancelación y pausa, y paridad de `MemoryStore`.
- Despliegue en `develop`, commit `1c970be`: se guardó respaldo PostgreSQL válido en `/var/backups/cloudtransfermanager/pre-20261018-transfer-incidents-pause.dump` (modo `0600`); se verificó que no hubiera transferencias activas, workers con tareas activas/reservadas ni mensajes pendientes antes de reconstruir. PostgreSQL quedó en `20261018_0017`; `/health/ready`, `/health/evaluations`, `/health/notifications` y el proxy HTTPS respondieron correctamente.
- No se probó una copia real ni se modificó `TRF-2026-000013`. La instalación queda lista para prueba funcional autenticada; validar la pausa con una transferencia dedicada antes de usarla operacionalmente.

## Fases del plan original

| Fase | Estado en el código | Nota |
|---|---|---|
| 1. Base del proyecto | Implementada | Monorepo, servicios, persistencia, lint y pruebas están presentes. |
| 2. Vertical slice con mocks | Implementada | El flujo mock sigue como soporte de desarrollo y pruebas. |
| 3. Autenticación, roles y CLI | Implementada | Incluye bloqueo por intentos y cambio inicial de contraseña. |
| 4. Proveedores y cifrado | Implementada | Se añadieron MEGAcmd, Seafile y SMB además de Google Drive. |
| 5. Explorador | Implementada | El diseño actual está descrito en la spec 03. |
| 6. Transferencias reales | Implementada | Incluye staging y manifiesto SMB, más programación durable. |
| 7. SSE y notificaciones | Implementada | Incluye progreso, recuperación del estado y administración SMTP. |
| 8. Auditoría y hardening | Parcial | La auditoría y sanitización existen; rate limiting, garantía de inmutabilidad en BD si se mantiene como requisito y evidencia final de aceptación requieren cierre. |
