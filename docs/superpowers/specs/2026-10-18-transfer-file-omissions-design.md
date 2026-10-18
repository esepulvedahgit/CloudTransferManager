# Omisión controlada de archivos y continuidad de transferencias

Estado: propuesta para revisión del usuario; no implementada ni desplegada.

## Objetivo y alcance

Continuar una copia cuando un archivo del origen no pueda descargarse, conservando el manifiesto, los nombres asignados y los archivos ya verificados. El usuario debe conocer exactamente qué no se copió. Primera entrega: omisión manual auditable y controles compartidos. Segunda entrega: omisión automática selectiva de restricciones MEGA confirmadas por archivo. Ambas entregas incluyen advertencias; la recuperación posterior y el informe completan el flujo.

Caso de referencia observado el 2026-10-18: `TRF-2026-000013`, MEGA → Seafile/Rclone, intento global 2, 1.977 archivos `COPIED`, un `FAILED` y 2.339 `PENDING`. El archivo fallido es `Curso Pimsleur.rar`, identidad `H:o4oAXA6K`, tamaño 686.289.042 bytes, etapa `SOURCE_DOWNLOAD`, `copy_started=false`. El usuario confirmó la restricción en MEGA. El diagnóstico histórico genérico y el código de proceso 16 no constituyen por sí solos una clasificación automática fiable. Ya se copiaron otros RAR de mayor tamaño.

## Decisiones de comportamiento

1. El archivo se conserva en el manifiesto y en el origen. La omisión aceptada usa `SKIPPED` y metadatos explícitos; nunca se registra como `COPIED`.
2. `omission_kind` distingue `USER_ACCEPTED_SOURCE_FAILURE` y `MEGA_FILE_RESTRICTED`. Los `SKIPPED` históricos sin este campo mantienen su significado y comportamiento previos.
3. La omisión manual exige transferencia `FAILED`, política `KEEP_BOTH`, origen MEGA, archivo `FAILED`, evidencia inequívoca de fallo individual en `SOURCE_DOWNLOAD`, `copy_started=false` y ausencia de ejecución/reserva vigente. Queda fuera de esta primera entrega la omisión de carpetas, SMB histórico y errores de otros proveedores.
4. No son omitibles errores generales de cuenta/autenticación/cuota, almacenamiento local, limpieza o destino incierto. Un código genérico puede aceptarse manualmente solo si cumple la evidencia anterior y el usuario confirma que continuará sin ese archivo; no se reclasifica retroactivamente como restricción confirmada.
5. La acción «Omitir este archivo y continuar» cambia el manifiesto, registra auditoría y encola el nuevo intento en una única transacción. Revalida propietario/ADMIN, acceso a ambas conexiones, conexiones activas y versiones de credenciales. La publicación Redis/Celery ocurre después del commit y es best-effort; Beat recupera la cola durable.
6. La reanudación omite `COPIED` y `SKIPPED` con `omission_kind` explícito. El inventario MEGA conserva su validación de identidad/ruta para todos los elementos, incluidos los omitidos. Eliminar o reemplazar el objeto de origen sigue provocando `MEGA_MANIFEST_REBUILD_REQUIRED`.
7. El resultado es `COMPLETED_WITH_WARNINGS` solo cuando no quedan `FAILED` ni `PENDING` y existen omisiones. Un error general o de integridad sigue terminando en `FAILED`. La limpieza debe terminar antes de continuar con otro archivo.
8. `bytes_transferred` cuenta exclusivamente archivos copiados/verificados. `bytes_total` no disminuye al omitir; `files_completed` conserva su semántica de elementos resueltos. El porcentaje de copia queda por debajo de 100 si hay omisiones, incluso con archivos de cero bytes. La UI explica «Procesamiento finalizado con advertencias» y muestra copiados/omitidos/fallidos/pendientes.
9. Un padre con omisiones explícitas no libera automáticamente a sus hijos. `BLOCKED_PARENT_OMISSIONS` exige aceptación por propietario del hijo o ADMIN para el intento concreto del padre. Las advertencias previas por renombrado/conflicto conservan las reglas actuales. Un nuevo intento del padre invalida la aceptación anterior.
10. La política automática por trabajo es `source_file_error_policy = STOP | SKIP_CONFIRMED_RESTRICTED`, por defecto `STOP` para trabajos nuevos e históricos. La UI permite elegir la segunda opción explícitamente en creación o reanudación; solo para origen MEGA y `KEEP_BOTH`. La omisión manual no activa por sí sola la automática.
11. Automático requiere diagnóstico normalizado explícito de restricción de archivo, alcance individual demostrado, descarga de origen y escritura no iniciada. Número de salida 16, extensión/tamaño, mensaje genérico de acceso y metadatos legibles no bastan. Si no hay firma verificada contra la versión instalada, la clasificación es `UNKNOWN` y el trabajo se detiene. No hay fallback de proveedor ni evasión de restricciones.
12. Tres omisiones automáticas consecutivas por la misma categoría/etapa detienen el trabajo con `TRANSFER_REPEATED_FILE_FAILURE`; una copia verificada reinicia la cuenta. Se conserva además el cortacircuito existente para `FAILED_SAFE`.

## Persistencia y compatibilidad

Migración propuesta `20261018_0015`, sobre `20261017_0014`; confirmar el head al ejecutar.

- `transfer_manifest_items`: `omission_kind VARCHAR(40) NULL`, `omitted_at TIMESTAMPTZ NULL`, `omitted_by UUID NULL REFERENCES users(id) ON DELETE SET NULL`, `last_failure_stage VARCHAR(32) NULL`, `last_failure_attempt INTEGER NULL`.
- `transfer_jobs`: `source_file_error_policy VARCHAR(32) NOT NULL DEFAULT 'STOP'`; `accepted_parent_omissions_attempt INTEGER NULL`.
- Restricciones: las omisiones explícitas son archivos `SKIPPED`, con fecha y escritura no iniciada; clases/políticas pertenecen a sus enums. Borrar un usuario conserva la omisión y su auditoría.
- Los campos nuevos tienen valores neutrales. No se altera masivamente el estado de transferencias o manifiestos históricos.
- El error original permanece en `error_code`; el motivo de omisión es un enum, sin notas de texto libre en logs/auditoría. No se guardan stdout/stderr crudos, credenciales ni rutas de staging.
- Los nuevos fallos incluyen UUID del manifiesto, etapa e intento global en eventos; el intento interno del archivo se guarda aparte como `file_attempt`.
- Compatibilidad del caso actual: resolver evidencia histórica solo cuando el último diagnóstico del intento vigente puede asociarse sin ambigüedad a un único archivo del manifiesto (basename único, código coincidente, etapa `SOURCE_DOWNLOAD`, proveedor MEGA y escritura no iniciada). No inferir por nombre si hay homónimos ni aceptar un diagnóstico anterior a `started_at`. Ante evidencia inconclusa, devolver motivo visible y exigir un diagnóstico nuevo; no editar SQL manualmente.

## API propuesta

Todas las mutaciones requieren sesión, CSRF y visibilidad del trabajo. Los cambios se serializan bloqueando transferencia, conexiones y entradas afectadas con orden consistente. Revalidar las condiciones dentro de la transacción, incluidos los accesos que exige la reanudación.

- `POST /api/v1/transfers/{id}/items/{item_id}/skip-and-resume`: `{expected_attempt: int, request_id: UUID, reason: 'USER_ACCEPTED_SOURCE_FAILURE', source_file_error_policy?: enum}`. Respuesta `202`, DTO de transferencia. `409` ante intento/estado obsoleto o entrada no elegible; `404` ante recurso no visible; `403` ante CSRF inválido. Entrada ajena al manifiesto se trata como no encontrada.
- `POST /api/v1/transfers/{id}/retry-omitted`: `{expected_attempt: int, request_id: UUID, item_ids: UUID[1..100]}`. Solo estados terminales `FAILED`, `CANCELLED`, `COMPLETED_WITH_WARNINGS`, sin reserva/ejecución y con las mismas comprobaciones de conexiones/permisos. Solo omisiones explícitas con escritura no iniciada; selección de IDs única. Restituye exclusivamente esos archivos a `PENDING`, política `STOP`, preserva identidad y destino, encola un intento nuevo. La auditoría conserva la omisión anterior aunque se limpien sus metadatos actuales.
- `POST /api/v1/transfers/{child_id}/accept-parent-omissions`: `{expected_parent_attempt: int, request_id: UUID}`. Hijo pendiente sin reserva; padre terminal con omisiones, relación vigente y visible para quien acepta. Guarda aceptación ligada al intento y auditoría, sin iniciar copias directamente. El dispatcher reevalúa después.
- `POST .../resume`: cuerpo opcional con `source_file_error_policy`; las llamadas existentes sin cuerpo conservan la política guardada.
- `GET .../items`: añade metadatos de omisión, `can_skip`, `skip_unavailable_reason` y `can_retry_omission`, calculados por el backend para el usuario actual. Nunca confiar en habilitación del botón como autorización.
- `GET /api/v1/transfers/{id}/omissions.csv`: informe de omisiones explícitas de un trabajo terminal visible, paginado internamente y sin cargar todo el manifiesto. UTF-8, columnas ruta de origen, destino asignado, motivo, fecha, tamaño. Escapar fórmulas de hoja de cálculo; nunca incluir secretos, rutas locales o salida cruda.
- REST/SSE conservan `manifest_summary`; añaden `omitted` como subconjunto de `skipped` (no se suman ambos) y `omitted_bytes`. Campos ausentes en respuestas antiguas equivalen a desconocido/cero según el DTO, sin rotular desconocidos como copiados.

Idempotencia: `request_id` se registra en el evento de auditoría con actor, recurso y resumen/hash del propósito. Dentro del bloqueo de la transferencia se detecta repetición: mismo propósito devuelve el resultado vigente sin otro incremento de intento; distinto propósito devuelve `409`. Auditoría y mutación confirman o revierten juntas. No implementar una secuencia cliente «omitir» y luego «reanudar» con commits independientes.

## Interfaz, advertencias y correo

- Informe por archivo reutiliza los componentes y Material UI actuales. Confirmación: «Este archivo no se copiará. Los archivos ya copiados se conservarán y la transferencia continuará con los restantes». Mostrar nombre/ruta y razón segura.
- Omisión automática es una elección explícita: «Continuar si MEGA restringe un archivo; finalizar con advertencias». Nunca preseleccionarla al actualizar trabajos históricos.
- Advertencia durante ejecución e historial, contador de omitidos, informe descargable y selección para reintentar omitidos. Control separado para aceptar un padre con omisiones, ligado al intento mostrado.
- Correo terminal incorpora conteos desde el snapshot inmutable del outbox del intento, no desde el estado actual del trabajo. Enlace al historial para el detalle, sin lista de rutas en el correo. Los snapshots antiguos siguen siendo entregables y muestran datos disponibles.

## Recuperación de TRF-2026-000013

Tras revisar, validar y desplegar el cambio, abrir su informe y verificar el UUID de la fila fallida y el diagnóstico del intento actual. El usuario ejecuta «Omitir este archivo y continuar». El cambio esperado conserva 1.977 copiados y bytes transferidos, transforma un fallo en omisión y deja 2.339 pendientes. Si todos los pendientes se copian: 4.316 copiados, un omitido y `COMPLETED_WITH_WARNINGS`. El plan no autoriza saltarse validaciones, eliminar el origen ni aplicar cambios directos a la base de datos.

## Criterios de aceptación

1. Omisión manual + reanudación atómicas, auditadas, idempotentes y sin repetir archivos copiados.
2. Las omisiones sobreviven reinicios/reentregas y no se reintentan con una reanudación normal.
3. Un resultado ambiguo de destino, fallo general o limpieza fallida no puede convertirse en omisión.
4. Compatibilidad histórica demostrada con una fixture saneada del segundo intento; homónimos y evidencia obsoleta rechazan la acción.
5. Finalización con advertencias y bytes honestos, incluso con omitidos de cero bytes.
6. Los hijos esperan aceptación explícita del intento con omisiones; evaluación y despacho final aplican la misma regla.
7. Automático opt-in y clasificación conservadora, cortacircuito y evidencia por versión.
8. UI, REST, SSE, CSV y snapshot de correo coinciden con el manifiesto.
9. Reintentar omitidos conserva asignaciones y no vuelve a copiar los `COPIED`.
