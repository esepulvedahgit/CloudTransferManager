# Evaluación anticipada de transferencias programadas

Fecha: 2026-10-17. Estado: diseño propuesto para implementación.

## Objetivo y evidencia

Al guardar una transferencia programada, evaluar en segundo plano si su selección cumple los límites y mostrar el conteo directamente en Programaciones. Una solicitud registrada no debe parecer aprobada antes de terminar esta evaluación.

TRF-2026-000008 fue tomada por el worker nueve segundos después de su hora y falló durante VALIDATING unos 24 segundos después: TRANSFER_FILE_LIMIT_EXCEEDED, máximo 5000. No copió archivos. El problema fue descubrir la incompatibilidad recién a la hora de ejecución.

## Experiencia de usuario

La fila de cada tarea en Programaciones presenta siempre un bloque de evaluación:

| Evaluación | Texto y conteo |
| --- | --- |
| PENDING | Pendiente de evaluación · cantidad todavía desconocida. |
| EVALUATING | Evaluando · 3.200 archivos encontrados · límite 5.000. |
| APPROVED | Programada · 4.180 archivos · conteo completo · fecha de evaluación. |
| BLOCKED | Bloqueada · al menos 5.001 archivos · límite 5.000 · divide la selección. |
| ERROR | No se pudo evaluar · motivo y acción Volver a evaluar. |
| WAITING_PARENT | Pendiente de evaluar tras la tarea padre. |
| NOT_REQUIRED | Sin evaluación anticipada · se validará al ejecutar. |

Los números anteriores son ejemplos; el límite procede del backend por proveedor. Solo se cuentan archivos únicos, incluidos los de subcarpetas; las carpetas no incrementan ese número. Un resultado completo vacío muestra cero; un conteo pendiente nunca muestra cero como si fuera definitivo. El conteo de evaluación es independiente de files_total/files_completed y del avance de copia.

La tarea aparece inmediatamente después de guardarla, aunque su fecha esté lejos. Se conserva visible si queda bloqueada. Se puede cancelar mientras se evalúa y volver a evaluar tras resolver un problema. Crear una nueva transferencia permite cambiar la selección; reprogramar solo modifica horario, modo o dependencia conforme al API actual.

Al pasar 60 segundos desde el inicio de la evaluación, mostrar «La revisión continúa; el tiempo depende de las carpetas y del proveedor». No prometer una respuesta en un minuto ni mostrar un porcentaje sin total conocido. Si la hora vence sin evaluación aprobada, indicar que espera la evaluación y mantener el horario solicitado.

## Estado, persistencia y compatibilidad

Mantener SCHEDULED/QUEUED y la máquina de ejecución existentes. Añadir una tabla transfer_evaluations con una fila por transferencia evaluada, UUID v4 propio y transfer_job_id único. Una fila ausente se publica como NOT_REQUIRED.

Campos: status, generation (entero que invalida intentos anteriores), sequence (orden de actualizaciones dentro de una generación), files_found, count_complete, max_files, source_credential_version, destination_credential_version, started_at, updated_at, completed_at, safe_error_code, safe_message, attempt_count, retry_at, claim_token y heartbeat_at. Las marcas temporales se guardan en UTC. files_found es un límite inferior hasta count_complete=true.

Crear transferencia y evaluación PENDING en la misma transacción para solicitudes con scheduled_at. El replay de Idempotency-Key devuelve la misma tarea y no crea otra evaluación. Al convertir una tarea pendiente inmediata en programada, crear su evaluación. Las transferencias inmediatas conservan el flujo actual.

La migración crea evaluaciones PENDING para tareas históricas SCHEDULED/QUEUED con scheduled_at y sin dispatch_token ni execution_started_at. No altera ejecuciones admitidas ni estados terminales. Nunca reencolar automáticamente TRF-2026-000008 ni otras tareas fallidas.

Una cancelación invalida la generación y elimina la reserva de evaluación; ninguna actualización posterior puede reactivar la tarea. Cambiar la dependencia invalida la evaluación y reinicia el conteo. Cambiar solo hora o modo conserva el resultado. Eliminar la fecha no elimina el requisito de evaluación de una tarea que ya lo tenía.

## Inventario compartido y límites

Extraer la enumeración de solo lectura de TransferExecutor._collect_versioned_tree a un servicio común. Mantener identidad por object_id cuando existe, paginación, detección de cursores repetidos, rutas válidas, selección solapada, nombres homónimos, cancelación y límites de tamaño existentes. Tanto la evaluación como la ejecución deben consumir esta misma lógica.

La evaluación enumera metadatos del origen; no descarga contenido, crea carpetas, asigna nombres definitivos, escribe el manifiesto de copia ni reserva staging. Comprueba también las reglas locales del par de proveedores, permisos de usuario y versiones de conexión. Una aprobación no certifica disponibilidad futura, cuota ni permisos efectivos de escritura del destino.

Usar TRANSFER_MAX_FILES=5000 por defecto y SMB_TRANSFER_MAX_FILES=100000 para origen SMB. Detener el recorrido al encontrar el archivo límite+1; guardar ese conteo con count_complete=false y BLOCKED. No aumentar los límites como parte de esta mejora. Tamaños desconocidos conservan las excepciones actuales para documentos Google.

Publicar conteos parciales como máximo una vez por segundo y publicar siempre el resultado final. Un heartbeat de cinco segundos protege la propiedad del intento incluso cuando no aparecen nuevos archivos. No reutilizar un conteo parcial como inventario definitivo de ejecución.

## Despacho, recursos y recuperación

Añadir una cola evaluations y un evaluation-worker con concurrencia uno, secretos y herramientas equivalentes a los necesarios para leer los proveedores. El control-worker solo reserva y publica tareas; nunca recorre árboles. Beat solicita el despacho cada 15 segundos y recupera reservas que no llegaron al broker.

La evaluación no consume TRANSFER_MAX_CONCURRENT ni el cupo de staging. Serializar evaluaciones sobre una misma conexión y posponerlas cuando una copia activa escribe en su origen. Aplicar las mismas comprobaciones de acceso y cambios de credenciales del scheduler. Los inventarios añaden lecturas remotas y consumo de CPU/memoria; limitar el worker y conservar sesiones MEGAcmd aisladas y limpieza garantizada.

Reservar en PostgreSQL con token y generación. Renovar heartbeat cada cinco segundos; una reserva sin heartbeat durante 60 segundos se invalida y se recupera. Usar bloqueo de ejecución por transferencia para que una redelivery o un proceso lento no deje dos evaluaciones activas. Las escrituras requieren token, generación y tarea no cancelada.

Límite de duración por intento: 600 segundos. Un fallo transitorio o timeout vuelve a PENDING con reintento tras 60 y 120 segundos; después de tres intentos queda ERROR, visible y sin ejecutar. La acción manual Volver a evaluar abre una generación nueva, con autorización y CSRF. Un error de conteo o tamaño confirmado queda BLOCKED. No tratar indisponibilidad como prueba de que el contenido es inválido.

Una tarea con padre no exitoso queda WAITING_PARENT antes de consultar proveedores. Tras COMPLETED o COMPLETED_WITH_WARNINGS se evalúa; padre fallido/cancelado mantiene el bloqueo de dependencia actual.

El dispatcher de copias solo admite tareas con evaluación APPROVED o NOT_REQUIRED. Las pendientes/bloqueadas no reservan tokens de copia ni activan una barrera EXCLUSIVE que frene trabajos posteriores. Si el límite o versiones guardadas ya no coinciden, no usar la aprobación; aplicar el bloqueo actual de credenciales o solicitar nueva evaluación por cambio de límite. Antes de copiar se repite la validación actual porque el origen puede haber cambiado.

## API y actualización de interfaz

Añadir un objeto evaluation a las respuestas de creación, consulta, listado y SSE. Publicar únicamente status, generation, sequence, files_found, count_complete, max_files, fechas, safe_error_code y safe_message; excluir tokens y credenciales. Hidratar listados en lote, evitando una consulta por fila.

Añadir POST /api/v1/transfers/{transfer_id}/evaluation para solicitar reevaluación: propietario o ADMIN, CSRF y estado pendiente sin despacho de copia. Devuelve 202 con la evaluación; rechaza tareas admitidas o terminales con 409. Repetir la petición mientras ya está pendiente/en curso no crea otro intento.

La UI combina SSE y polling de 15 segundos usando (generation, sequence), sin regresar a un conteo antiguo. Un número menor solo es válido al empezar una generación nueva. Reabrir la página reconstruye el conteo desde PostgreSQL. Los bloqueos siguen en Programaciones mediante SCHEDULED/QUEUED; no se convierten en FAILED de copia. El reloj de segundo plano los sigue incluyendo. Mantener el comportamiento de estados terminales del flujo existente.

## Validación y despliegue

Pruebas de límites exactos, carpetas anidadas, identidad y selección solapada; reservas duplicadas, reinicios, cancelación, publicaciones fallidas, dependencias y barrera exclusiva; autorización y replay; interfaz con conteo parcial/completo y SSE atrasado. Usar SQLite para comportamiento local y PostgreSQL para garantías de concurrencia.

Preparar código, migración, documentación y pruebas antes del despliegue. Ensayar la migración en una base desechable. Aplicar en una ventana sin trabajos admitidos, iniciando el evaluation-worker junto con las versiones compatibles de API y scheduler. Verificar primero con mocks; una prueba de lectura contra proveedores reales requiere el acceso operativo de la instalación. No realizar copias reales como efecto de evaluar.
