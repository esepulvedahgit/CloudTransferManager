# Evaluación de resiliencia e inconsistencias

Fecha: 2026-10-14, America/Santiago. Base revisada: `92c6d42` y árbol de trabajo local. Alcance: API, repositorio SQL, ejecución de transferencias, Celery/Redis, telemetría, frontend y despliegue Docker.

**Dictamen:** la aplicación dispone de mecanismos útiles de recuperación, pero conserva riesgos altos de inconsistencia ante concurrencia y fallos parciales. Recomiendo mantenerla en uso supervisado y cerrar los hallazgos P1 antes de migraciones críticas sin supervisión o de aumentar la concurrencia. La mayor necesidad es proteger la coordinación y la persistencia; cambiar React, FastAPI o Celery no resuelve por sí mismo estos problemas.

## Método y límites

Se revisaron README, especificaciones 04/05/07, programación, estado de implementación, código y pruebas existentes. Se consultó el estado del despliegue y configuración no sensible de Redis/Docker. Se realizaron siete comprobaciones aisladas mediante código real del backend, SQLite en memoria, HTTP ASGI y proveedores/subprocesos simulados. No se modificaron registros de PostgreSQL del despliegue, secretos, conexiones ni archivos de proveedores. No se detuvieron servicios ni se implementaron correcciones de producto durante esta evaluación.

Los resultados reproducidos confirman las secuencias probadas, no su frecuencia en producción. Los riesgos de pérdida de sesión PostgreSQL, agotamiento de recursos y pérdida de eventos del navegador se deducen del código y requieren ensayos de fallos en un entorno de pruebas. No se ejecutaron la suite completa, pruebas de carga ni copias con proveedores reales. SQLite no valida advisory locks ni el aislamiento de PostgreSQL.

Prioridad P1: cerrar antes de operación crítica; P2: siguiente etapa de endurecimiento. Probabilidad es una estimación cualitativa según el desencadenante, no una tasa medida. El impacto indicado corresponde al escenario descrito.

## Fortalezas existentes

- PostgreSQL conserva la intención de transferencia y la programación. Beat vuelve a revisar pendientes cada 15 segundos.
- El dispatcher reserva antes de publicar a Celery, descarta tokens antiguos y recupera reservas sin inicio después de 120 segundos. Serializa admisiones con un advisory lock transaccional.
- El worker usa un lock de ejecución por transferencia; Celery tiene confirmación tardía, rechazo por pérdida del worker y prefetch de uno.
- Hay comprobación de permisos y versión de credenciales antes de empezar, estados terminales y diagnóstico de ejecuciones interrumpidas.
- SMB dispone de manifiesto persistente, resultados por archivo e inserciones deduplicadas. Existen verificaciones posteriores de destino; Seafile API y ciertas exportaciones usan SHA-256.
- Se evita reintentar automáticamente una subida MEGA cuyo resultado es ambiguo. La aplicación preserva los archivos de origen y restringe los motores a operaciones autorizadas.
- Solo el proxy publica puertos; PostgreSQL y Redis tienen volúmenes persistentes. En la consulta realizada, los ocho servicios estaban activos y los servicios con healthcheck estaban saludables.

## Matriz de riesgos

| ID | Prioridad | Probabilidad estimada | Impacto | Debilidad |
|---|---|---|---|---|
| R1 | P1 | Media con solicitudes concurrentes | Alto | Escrituras atrasadas pueden revertir estados terminales o de cancelación. |
| R2 | P1 | Media con administración concurrente al login | Alto | Guardar un usuario completo puede revertir desactivaciones y cambios de contraseña. |
| R3 | P1 | Media durante fallos parciales de BD/Redis | Alto | La telemetría puede convertir una copia terminada en fallida o bloquear la ejecución. |
| R4 | P1 | Media ante timeout o error de respuesta | Alto | Una solicitud fallida puede haber creado el trabajo; repetirla crea otro. |
| R5 | P1 | Media durante pérdida de señal Redis | Alto | La intención durable de cancelar no es la fuente de consulta del worker. |
| R6 | P1 para migraciones críticas | Dependiente del proveedor y escrituras externas | Alto | La reconciliación por tamaño puede aceptar un archivo diferente como copiado. |
| R7 | P1 antes de escalar/failover | Baja en operación normal; relevante al reiniciar BD | Alto | Pérdida de lock o heartbeat no detiene de forma supervisada al ejecutor anterior. |
| R8 | P2 | Media durante cuelgues o caída de Beat/worker específico | Alto | Salud del proceso no demuestra progreso ni capacidad efectiva por cola. |
| R9 | P2; P1 antes de archivos grandes | Crece con duración y volumen | Alto | Recursos sin límites y staging huérfano pueden agotar el host compartido. |
| R10 | P1 si no existe respaldo externo verificado | No determinada | Muy alto | Recuperación de BD y clave maestra no demostrada. |
| R11 | P2 | Media ante fallos del broker/SMTP | Medio | Notificaciones sin registro durable de entrega; comparten la cola de copias. |
| R12 | P2 | Media al perder eventos SSE | Medio | El estado local del frontend puede prevalecer sobre una consulta más reciente. |

## Hallazgos y mejoras

### R1. Transiciones de transferencia sin protección frente a escrituras atrasadas

**Evidencia:** [`asave_transfer`](../../backend/app/services/database_store.py#L1221) copia estado, métricas y programación desde un objeto leído previamente, sin versión, condición de estado previo ni validación del token. La API vuelve a invocarlo desde [`publish_transfer`](../../backend/app/main.py#L476), incluso después de operaciones que ya guardaron el cambio en su propia transacción.

**Reproducción:** después de persistir `CANCELLING`, guardar la copia antigua del worker devolvió el trabajo a `RUNNING`. En otra secuencia, guardar una respuesta atrasada de cancelación después de `COMPLETED` produjo `CANCELLING` con `completed_at=None`. Esto puede dejar historia contradictoria, trabajos aparentemente activos y reservas sin liberar correctamente.

**Mejora:** separar publicación de persistencia; actualizar solo los campos de cada operación. Añadir una revisión monotónica y actualizaciones condicionales por revisión, estado esperado y token de intento. Definir las transiciones permitidas: una actualización de progreso no debe sacar un trabajo de un estado terminal ni borrar una cancelación. Guardar transición y evento durable en una transacción. Rechazar y registrar escrituras obsoletas, sin reintentarlas ciegamente.

**Aceptación:** entrelazar progreso, cancelación, finalización y reprogramación con PostgreSQL real; ninguna escritura antigua puede revertir una transición posterior ni alterar un intento nuevo.

### R2. Cambios de usuario que se pierden durante otra operación

**Evidencia:** [`asave_user`](../../backend/app/services/database_store.py#L318) actualiza también contraseña, rol y activación cuando solo se necesita cambiar métricas de login. El [login](../../backend/app/main.py#L635) guarda el usuario completo que leyó antes de verificar la contraseña.

**Reproducción:** un objeto leído antes de una desactivación y un restablecimiento de contraseña volvió a guardarse para actualizar `last_login_at`. El usuario terminó activo y con el hash anterior. La comprobación del [último administrador](../../backend/app/main.py#L827) también cuenta y guarda en operaciones distintas; queda expuesta a decisiones concurrentes basadas en el mismo conteo.

**Mejora:** métodos específicos para login, bloqueo, contraseña y administración; ninguna actualización de login debe escribir campos administrativos. Proteger cambios sensibles por versión y revalidar activación/versión de autenticación antes de conceder la sesión. Serializar en una transacción la regla de conservar al menos un administrador activo. Los incrementos de intentos fallidos también deben ser atómicos.

**Aceptación:** un login concurrente nunca reactiva una cuenta, restaura una contraseña o revierte su rol; dos operaciones simultáneas no pueden eliminar al último administrador.

### R3. Fallar al informar progreso puede alterar el resultado de la copia

**Evidencia:** el [`publish` del worker](../../backend/app/worker.py#L67) guarda transferencia, inserta evento y publica en Redis, propagando los fallos de cualquiera de esos pasos. El [`execute`](../../backend/app/services/transfer_executor.py#L120) captura excepciones de ejecución y llama a `_fail`, que vuelve a guardar `FAILED` sin distinguir un error del motor de un error al notificar su resultado.

**Reproducción:** se simuló un motor que acababa correctamente y una excepción de telemetría después de guardar `COMPLETED`. El resultado persistido terminó en `FAILED`, conservando un archivo completado. No fue una prueba de copia contra un proveedor real; sí del tratamiento real de la excepción en el ejecutor.

Hay además un fallo de supervisión en el [lector de salida Rclone](../../backend/app/services/rclone.py#L497): si el callback falla, el bucle espera al proceso sin observar inmediatamente esa excepción. Un subproceso simulado siguió esperando y solo terminó por el timeout externo de la prueba. Con salida suficiente, dejar de drenar el pipe también puede bloquear al proceso. MEGAcmd presenta un patrón similar que merece la misma revisión; no se reprodujo ese segundo motor.

**Mejora:** aislar el resultado de negocio del canal SSE. Guardar estado y evento durable de forma atómica y distribuir después con reintentos. Supervisar proceso, lectores y callbacks conjuntamente; si falla la persistencia crítica, detener el motor de forma controlada y registrar un resultado incierto que exija reconciliación. Si falla solo la entrega de progreso, conservar el resultado durable y recuperar después la distribución. La disponibilidad de telemetría no debe decidir si el archivo se copió.

Redis Pub/Sub puede perder mensajes y no ofrece repetición de lo que un suscriptor no recibió; esta semántica refuerza la necesidad de separar eventos de interfaz y verdad persistida. Véase la [documentación de Redis Pub/Sub](https://redis.io/docs/latest/develop/pubsub/).

**Aceptación:** desconectar el publicador en mitad de una copia y al emitir su finalización no produce una falsa transición de éxito a fallo; una excepción del lector no deja un subproceso sin supervisión.

### R4. Respuesta fallida después de crear el trabajo, sin idempotencia

**Evidencia:** [`acreate_transfer`](../../backend/app/services/database_store.py#L529) confirma el trabajo antes de que el [endpoint publique y audite](../../backend/app/main.py#L1614). No se observa una clave de idempotencia ni una restricción que relacione reintentos de la misma solicitud. La misma ambigüedad existe si se pierde la respuesta HTTP después de confirmar.

**Reproducción:** mediante la API ASGI y DatabaseStore sobre SQLite en memoria, dos solicitudes idénticas con fallo simulado del publicador devolvieron `[500, 500]`; quedaron **dos trabajos persistidos**. El usuario puede ver un error y volver a ordenar una copia que ya estaba programada.

**Mejora:** aceptar una clave de idempotencia generada por el cliente y conservada para reintentar la misma intención. Guardarla junto al hash del payload y la transferencia bajo unicidad por usuario; misma clave/payload devuelve el mismo trabajo y un payload diferente devuelve conflicto. Guardar trabajo, auditoría crítica e intención de publicación en una transacción mediante una tabla de salida pendiente —outbox—. Reintentar su entrega con deduplicación.

El dispatcher ya compensa reservas sin inicio tras 120 segundos; no corresponde afirmar que toda caída entre PostgreSQL y Celery pierde el trabajo para siempre. La brecha es la respuesta ambigua, sus efectos duplicados y la publicación de progreso previa al [envío de las reservas a Celery](../../backend/app/worker.py#L159).

**Aceptación:** repetir la solicitud tras perder una respuesta o fallar Redis siempre devuelve el mismo UUID y código público para la misma clave de idempotencia.

### R5. Cancelación durable registrada, pero señal efectiva temporal

**Evidencia:** [`acancel_transfer`](../../backend/app/services/database_store.py#L981) guarda `cancel_requested_at`; el [`should_cancel` del worker](../../backend/app/worker.py#L89) consulta únicamente Redis. La [clave Redis](../../backend/app/services/event_bus.py#L26) expira después de una hora. La escritura en PostgreSQL y la [señal Redis de la API](../../backend/app/main.py#L2009) son pasos separados.

Si Redis pierde la clave, esta expira o la API falla antes de enviarla, la cancelación puede quedar registrada y no ser obedecida. El posterior progreso también puede sobrescribir `CANCELLING`, como confirma R1. No se provocó una pérdida real de Redis en el despliegue.

**Mejora:** usar PostgreSQL como autoridad de la intención de cancelar; Redis puede acelerar el aviso. Consultar de forma acotada y periódica el intento vigente y la cancelación persistida, incluso durante reintentos e inventarios. Limpiar señales por intento, evitando que la limpieza de un worker antiguo afecte una reanudación. Definir qué operaciones remotas pueden seguir completándose tras la cancelación y reconciliarlas.

**Aceptación:** una cancelación confirmada se mantiene efectiva aunque desaparezca toda su señal Redis; el plazo máximo hasta detener la copia debe medirse y documentarse por proveedor.

### R6. Igual tamaño no demuestra que sea el archivo correcto

**Evidencia:** la [reconciliación SMB](../../backend/app/services/transfer_executor.py#L674) puede devolver `COPIED/SIZE` si una copia había empezado, el destino no existía al inicio y ahora tiene el tamaño esperado. Esa rama se aplica también antes del flujo de verificación de Seafile API. La [verificación Rclone](../../backend/app/services/transfer_executor.py#L2663) de la aplicación comprueba presencia y tamaño.

**Reproducción:** origen simulado `AAAA` y destino `BBBB`, ambos de cuatro bytes, se aceptaron como `COPIED`, verificado por `SIZE`. Esto no demuestra corrupción causada por el motor; demuestra que el reconciliador no puede distinguir contenido incorrecto del correcto en ese caso. Rclone puede aplicar comprobaciones propias según el proveedor, pero esa rama de reconciliación no las ejecuta.

**Mejora:** guardar identidad del objeto, versión/ETag útil, tamaño, marcas de tiempo y checksum cuando esté disponible. Ante un resultado remoto ambiguo, exigir identidad o verificación de contenido; si no se puede demostrar, mantenerlo como pendiente de reconciliación. Para datos críticos, permitir verificación por lectura SHA-256 con costo de tiempo y tráfico explícito. Usar snapshots de origen o detectar cambios antes y después de leer. Extender el manifiesto durable a flujos cloud que actualmente necesitan reiniciar más trabajo.

La solución debe respetar `COPY`, la lista blanca de comandos y la preservación del origen. No requiere introducir borrados, movimientos ni reintentos automáticos de una subida MEGA incierta.

**Aceptación:** dos archivos de igual tamaño y diferente contenido nunca se presentan como copia verificada en el modo de integridad exigido; una recuperación ambigua no vuelve a subir sin verificar el destino.

### R7. Lock de sesión y heartbeat sin supervisión suficiente del ejecutor

**Evidencia:** el [lock de ejecución](../../backend/app/services/database_store.py#L71) se mantiene en una conexión dedicada. El [heartbeat](../../backend/app/worker.py#L92) usa otras sesiones; si devuelve falso acaba, y si lanza una excepción la tarea queda fallida sin detener automáticamente al ejecutor. Su resultado se recoge al final con `return_exceptions=True`. Las escrituras de progreso tampoco comprueban el token, como en R1.

**Escenario inferido:** un reinicio de PostgreSQL libera el lock y rompe el heartbeat; un proceso de transferencia aún vivo puede continuar escribiendo en el proveedor. Tras recuperar la BD, el dispatcher podría declarar el intento interrumpido y liberar capacidad mientras la ejecución anterior todavía existe. La documentación de [advisory locks de PostgreSQL](https://www.postgresql.org/docs/16/explicit-locking.html) confirma que los locks de sesión terminan al cerrarse esta. No se ensayó esta secuencia contra PostgreSQL real.

**Mejora:** supervisar pérdida de lock, renovación y ejecución como una unidad. Rechazar toda persistencia de un token/revisión antiguos. Detener subprocesos cuando no sea posible confirmar la propiedad de ejecución y bloquear una nueva escritura sobre el mismo recurso hasta comprobar su terminación o reconciliar el destino. Un token de BD no impide por sí solo que un proceso antiguo siga escribiendo en un proveedor que no conoce ese token.

**Aceptación:** en un ensayo de reinicio/partición de BD no coexisten dos escritores autorizados sobre el mismo destino y los mensajes de intentos anteriores no cambian el nuevo estado. Si se eleva la concurrencia, considerar también conexiones distintas que representan el mismo recurso físico: el control actual compara UUID de conexión.

### R8. Detección de proceso vivo, sin demostrar que el sistema avanza

**Evidencia:** [`/health/ready`](../../backend/app/main.py#L558) exige que responda algún worker, sin comprobar los consumidores necesarios de cada cola ni la actividad reciente de Beat. Los healthchecks de worker, control-worker y Beat están [desactivados](../../compose.yaml#L80). Fuera de Nextcloud no hay deadline global uniforme; las [copias MEGA](../../backend/app/providers/megacmd.py#L254) no reciben el timeout de metadatos.

Un worker bloqueado puede seguir respondiendo a control. Con el límite predeterminado de una transferencia, un cuelgue basta para parar toda la cola. El timeout de visibilidad de 24 horas de Celery no sustituye un límite de ejecución ni demuestra avance. La [documentación de Celery sobre Redis](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html) explica que un timeout alto también retrasa la reentrega de mensajes perdidos; los tokens y la reconciliación de la aplicación deben seguir siendo la defensa contra duplicación.

**Mejora:** indicadores separados para API, colas, dispatcher y ejecución; registrar último despacho exitoso, antigüedad de pendientes, heartbeat y avance real. Añadir límites configurables por operación e inactividad, con distinción entre proveedor lento y proceso bloqueado. Configurar parada gradual de contenedores y probarla; confirmar el fin de procesos/daemons antes de liberar recursos. Mantener fuera del servicio una alerta que detecte su propia caída.

**Aceptación:** detectar por separado ausencia del consumidor `transfers`, del consumidor `control`, caída de Beat y un trabajo vivo sin progreso; cada caso produce una alerta y una recuperación segura definida.

### R9. Agotamiento de recursos y temporales huérfanos

**Observado:** Redis tenía `maxmemory=0`, política `noeviction` y `appendfsync=everysec`. El worker tenía límite Docker de memoria `0` y logging `json-file` sin opciones de rotación. Compose no fija presupuestos de CPU/memoria ni rotación. El [staging](../../backend/app/services/staging.py#L49) se limpia en `finally`, que no corre tras SIGKILL; el entrypoint crea directorios pero no reconcilia los huérfanos. Reiniciar el mismo contenedor conserva su capa writable.

**Mejora:** presupuestos por servicio con margen suficiente, rotación de logs y alertas sobre disco, inodos, memoria y colas. Limitar staging y recoger huérfanos únicamente después de comprobar propietario, intento y ausencia de procesos activos; no borrar indiscriminadamente HOME de MEGAcmd. Separar el dominio de fallo del disco de datos respecto a temporales cuando se diseñe su almacenamiento. Revisar también el crecimiento de eventos por segundo y que los listados se paginen en SQL: [`alist_transfers`](../../backend/app/services/database_store.py#L1194) actualmente carga todos los resultados antes de paginar en la API.

AOF `everysec` reduce la pérdida, pero puede perder aproximadamente el último segundo ante un desastre; no debe ser el único registro de intención de tareas y cancelaciones. Véase [persistencia de Redis](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/).

**Aceptación:** al llenar el presupuesto de staging en un entorno de pruebas, se rechaza la copia y PostgreSQL mantiene capacidad de escribir; una caída abrupta no acumula temporales indefinidamente.

### R10. Recuperación de desastre sin evidencia de restauración

Los volúmenes [PostgreSQL/Redis](../../compose.yaml#L183) sobreviven a recreaciones normales, pero no son un respaldo externo. El README exige preservar BD y `master_key`; no se encontró automatización de respaldo/restauración en el repositorio. Esto no demuestra que no exista fuera del proyecto: debe confirmarse con la operación del host.

**Mejora:** respaldos cifrados fuera del servidor, retención y pruebas de restauración con la misma clave maestra. Definir pérdida máxima aceptable —RPO— y tiempo máximo de recuperación —RTO—. Como objetivo inicial a evaluar: RPO de 15 minutos y RTO de dos horas, sujetos a costo y ensayo real; para ese RPO puede usarse archivado WAL y recuperación a un momento concreto, documentada por [PostgreSQL 16](https://www.postgresql.org/docs/16/continuous-archiving.html).

**Implementación posterior:** se agregó un servicio opt-in para `pg_dump` cifrado a MEGA S4 cada 10 minutos, una prueba aislada con instancia PostgreSQL separada y un procedimiento que marca trabajos en curso para revisión antes de habilitar workers. Quedan pendientes credenciales/endpoints del bucket, una restauración con datos de la instalación, medir RPO/RTO y acordar retención. Este mecanismo usa snapshots lógicos; no archiva WAL.

Una restauración debe arrancar con admisiones detenidas y reconciliar trabajos, mensajes del broker y estado real de los proveedores. Restaurar una foto antigua de la BD y reanudar automáticamente puede repetir operaciones que sí llegaron al destino después del respaldo.

**Aceptación:** restaurar en un entorno separado, descifrar credenciales de prueba y reconciliar una transferencia interrumpida sin duplicar la escritura remota. Registrar tiempos y pérdida real observada; no declarar cumplidos RPO/RTO solo por tener un backup.

### R11. Notificaciones que se pierden o duplican y compiten con las copias

**Evidencia:** [`enqueue_transfer_notification`](../../backend/app/services/notifications.py#L47) solo invoca `task.delay`; no persiste pendiente/enviada/resultado incierto. La [ruta Celery](../../backend/app/worker.py#L34) usa `transfers`, igual que las copias. Si el worker muere después del estado terminal y antes de encolar, no hay un recuperador de correo pendiente. Tras una aceptación SMTP con respuesta perdida puede haber un envío duplicado al reintentar.

**Mejora:** outbox transaccional por transferencia/intento/evento terminal, estado de entrega y cola separada para correo. Mantener la deduplicación local y reconocer la ambigüedad externa de SMTP: no prometer entrega exactamente una vez. Un fallo de correo nunca debe cambiar el resultado de la copia. Exponer pendientes y fallos de notificación al administrador.

**Aceptación:** matar el proceso entre finalización y encolado no pierde la intención de correo; los fallos SMTP no bloquean la capacidad de copias ni alteran su estado.

### R12. Interfaz con estado local que puede ocultar una consulta posterior

**Evidencia:** el [historial](../../frontend/src/App.tsx#L1833) superpone `activeTransfer` local al registro obtenido por polling. La [reconciliación de esa variable](../../frontend/src/App.tsx#L1886) cubre casos particulares, pero no actualiza de forma general un `RUNNING` local al terminal consultado para una transferencia inmediata. SSE aplica mensajes sin revisión monotónica; un evento publicado antes de la lectura del snapshot puede llegar después de ese snapshot.

**Mejora:** incluir la revisión de R1 en REST y SSE y aplicar solo revisiones posteriores. Reconciliar todas las transferencias activas con las consultas periódicas, incluso si falta un evento terminal; mostrar desconexión o datos atrasados. Antes de reintentar una creación ambigua, recuperar el trabajo por la clave de idempotencia.

**Aceptación:** perder el evento final, reconectar y recibir un evento atrasado no deja un trabajo terminado mostrado indefinidamente como activo ni provoca una nueva copia por error del usuario.

## Resultados de las comprobaciones aisladas

| Caso | Resultado observado |
|---|---|
| Guardar progreso atrasado después de cancelar | `CANCELLING → RUNNING`; el campo durable de cancelación seguía presente. |
| Guardar respuesta atrasada de cancelación después de finalizar | `COMPLETED → CANCELLING`, con `completed_at=None`. |
| Repetir POST tras error del publicador | HTTP `[500, 500]`; dos trabajos en el repositorio SQL en memoria. |
| Fallar telemetría después de finalización simulada | Motor simulado `COMPLETED`; persistencia final `FAILED`, un archivo completado. |
| Fallar callback del lector Rclone con subproceso aún vivo | El error no se propagó antes del timeout externo de un segundo; la prueba canceló y terminó el subproceso. |
| Reconciliar `AAAA` contra `BBBB`, igual tamaño | `COPIED`, `verified_by=SIZE`, aunque el contenido era distinto. |
| Guardar snapshot antiguo de login tras cambio administrativo | Cuenta nuevamente activa; cambio de hash no preservado. |

Estas son reproducciones de defectos, no pruebas que certifiquen soluciones: no se aplicaron arreglos como parte del informe.

## Orden recomendado de intervención

1. **Integridad de las decisiones:** corregir R1 y R2 con actualizaciones específicas, revisiones y transacciones. Resolver R5 usando la cancelación durable. Son cambios de base que evitan que otros mecanismos de recuperación vuelvan a introducir estados antiguos.
2. **Fronteras de fallo:** corregir R3 y R4; persistir resultados e intenciones de entrega de forma atómica, introducir idempotencia y supervisar lectores y subprocesos. Aplicar el mismo patrón a R11. El aumento de reintentos por sí solo elevaría el riesgo de duplicación.
3. **Recuperación de ejecución y datos:** cerrar R6/R7 con reconciliación verificable y pruebas PostgreSQL reales. Diseñar para entrega al menos una vez con efectos idempotentes; ante resultados que el proveedor no permite demostrar, detener y solicitar reconciliación, sin afirmar ejecución exactamente una vez entre dos clouds.
4. **Operación comprobable:** resolver R8/R9/R10 y R12. Confirmar inmediatamente que hay respaldo externo y restauración usable, en paralelo a las correcciones. Mantener concurrencia en uno hasta validar la coordinación, incluyendo recursos físicos registrados como conexiones distintas.

## Validación necesaria antes de una operación crítica

Los criterios de cierre anteriores deben probarse en un entorno separado con PostgreSQL, Redis y Celery reales. El CI actual ejecuta principalmente pruebas SQLite/mocks; la prueba PostgreSQL existente de archivado se omite si no hay `TEST_DATABASE_URL`, y el workflow revisado no la configura. Añadir este entorno es necesario para verificar locks, carreras y recuperación.

La batería mínima debe cubrir: pérdida de respuesta después de commit; cancelación frente a finalización; caída o pérdida de señal Redis; reinicio de PostgreSQL durante copia; muerte del proceso worker y del contenedor; pérdida del evento SSE terminal; archivo del mismo tamaño pero contenido distinto; modificación de origen; staging lleno; caída de Beat/consumidor de cada cola; y restauración de BD junto con clave maestra. Los resultados esperados son una sola intención de trabajo por clave, ausencia de escritores antiguos autorizados, estados terminales estables, resultado remoto reconciliado y ausencia de éxito sin la verificación exigida.

**Recomendación de uso actual:** piloto supervisado, origen conservado y verificación del destino antes de dar por terminada una migración. Para copias críticas sin supervisión, cerrar primero R1–R7 y demostrar restauración R10. Escalar recursos o agregar réplicas antes de esto puede aumentar la concurrencia de los fallos y su dificultad de diagnóstico.
