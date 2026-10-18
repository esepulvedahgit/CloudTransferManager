# Programar transferencias

Cloud Transfer Manager admite copias programadas una sola vez. La fecha se introduce en la zona local del navegador y se guarda normalizada a UTC junto con el identificador de zona IANA. No hay recurrencia en esta versión.

## Crear una programación

1. En el explorador, selecciona los archivos o carpetas y el destino.
2. Elige **Programar transferencia**.
3. Indica fecha y hora futuras, el modo de ejecución y, de manera opcional, una transferencia padre.
4. Consulta el estado y motivo de espera en **Programaciones**. Desde ahí puedes reprogramar o cancelar los trabajos todavía pendientes.

La sección recorre todas las páginas de trabajos pendientes y excluye las transferencias ya finalizadas.

## Evaluación anticipada del conteo

Al guardar una tarea con fecha futura, el sistema inicia un conteo de solo lectura en segundo plano. La tarea aparece enseguida en Programaciones con el estado del conteo; no se descargan archivos, no se crea el manifiesto de copia y no se reserva una posición de ejecución.

- **Pendiente de evaluación:** todavía no se conoce la cantidad. No representa cero.
- **Evaluando:** muestra un límite inferior de archivos encontrados y el máximo efectivo. Las carpetas no cuentan y las selecciones superpuestas no duplican archivos.
- **Aprobada:** muestra el total, la hora en que terminó la evaluación y el máximo vigente.
- **Bloqueada:** muestra el conteo parcial o completo, el límite y el motivo seguro. La tarea permanece en Programaciones y no se despacha al worker de copia.
- **Error:** indica que no se pudo terminar el conteo. Se puede solicitar **Volver a evaluar** después de corregir la causa.
- **Pendiente tras la tarea padre:** no se consulta el proveedor hasta que la transferencia padre termine correctamente.

El conteo se limita a `TRANSFER_MAX_FILES` (por defecto 5000) en orígenes cloud y `SMB_TRANSFER_MAX_FILES` (por defecto 100000) en orígenes SMB. El sistema se detiene al encontrar el archivo límite+1 y comunica que el conteo es parcial. El tiempo depende del número de carpetas, la paginación y la respuesta del proveedor; después de 60 segundos se informa que la revisión continúa, sin prometer una hora de término.

Si llega la fecha antes de que la evaluación se apruebe, la tarea conserva su horario y espera. Cambiar solo la fecha o el modo conserva la aprobación; cambiar la dependencia inicia una generación nueva. Convertir una tarea pendiente en programada crea la evaluación, y quitar la fecha más tarde no permite saltársela. Las tareas inmediatas sin evaluación anticipada conservan el preflight del worker de copia.

El propietario o ADMIN puede solicitar una generación nueva con `POST /api/v1/transfers/{transfer_id}/evaluation`; requiere CSRF. Las solicitudes repetidas mientras una generación está pendiente o ejecutándose no duplican el trabajo. La aprobación comprueba el inventario actual del origen, pero no garantiza que el proveedor, la cuota o los permisos de escritura del destino sigan disponibles al ejecutarse.

## Seguimiento en segundo plano

Las transferencias programadas, exclusivas, dependientes o en espera no crean tarjetas grandes de progreso ni aparecen en **Historial reciente** mientras sigan activas. El reloj pequeño del encabezado muestra cuántas hay en segundo plano. Al abrirlo, puedes revisar el motivo de espera o, cuando ya se ejecutan, los bytes transferidos y el porcentaje.

Las tareas futuras y en cola conservan en **Programaciones** las acciones disponibles para reprogramar o cancelar. Las ejecuciones activas también permanecen allí como filas de estado, sin una tarjeta de progreso; desde la sección se puede solicitar su cancelación. La sección pagina los trabajos activos y permite cargar páginas anteriores para conservar accesibles las acciones de cancelación aunque existan más de 200 tareas recientes. Cuando una ejecución termina, desaparece del indicador y pasa al historial con su estado definitivo. La UI recibe actualizaciones inmediatas por SSE y vuelve a consultar cada 15 segundos si se perdió un evento; el sondeo se pausa cuando la pestaña queda en segundo plano.

El historial muestra los conteos del manifiesto común (`copied`, `failed`, `skipped`) junto al avance en bytes. Cada fila puede mostrar el nombre solicitado y el asignado. `files_completed` cuenta elementos resueltos y no se debe interpretar como cantidad de archivos copiados.

Si Cloud Transfer Manager estuvo detenido cuando llegó la hora, Beat recupera el trabajo en el siguiente ciclo. La espera normal del dispatcher es de hasta 15 segundos, más el tiempo que demore en liberarse la capacidad o la dependencia.

## Modos de ejecución

- **Automática (`AUTO`)**: respeta `TRANSFER_MAX_CONCURRENT`, los conflictos de lectura/escritura por conexión y el cupo de staging. Las transferencias nuevas de todos los proveedores usan staging por archivo y comparten su cupo; los trabajos históricos conservan la ruta de ejecución que corresponde a su política.
- **Exclusiva (`EXCLUSIVE`)**: espera a que terminen los trabajos ya admitidos. Cuando llega a la cabeza de la cola, impide admitir trabajos posteriores hasta finalizar. Si se programa para el futuro, no bloquea la cola antes de su hora.

Las transferencias nuevas `KEEP_BOTH` de cualquier par consumen el cupo compartido de staging. Esto puede serializar transferencias simultáneas mientras un trabajo mantiene staging reservado.

## Dependencias

Una transferencia hija comienza después de que su padre termina en `COMPLETED` o `COMPLETED_WITH_WARNINGS` sin omisiones explícitas. Si el padre tiene archivos omitidos, el hijo queda en `BLOCKED_PARENT_OMISSIONS`; si el padre falla o se cancela, permanece bloqueado por seguridad. Revisa el informe del padre y acepta explícitamente las omisiones del intento mostrado si la copia parcial es válida para el hijo. La aceptación queda ligada a ese `attempt_count`; si se reanuda o reintenta el padre, el hijo vuelve a requerir revisión. Evaluación, dispatcher y worker revalidan el permiso antes de iniciar.

Una omisión manual desde el informe de archivos confirma que ese archivo no se copiará y reanuda el trabajo conservando las copias verificadas. El historial muestra `omitted` separado de `SKIPPED` por conflictos anteriores y de `failed`. En transferencias terminales se puede descargar el CSV de omisiones o reintentar selectivamente hasta 100 archivos explícitamente omitidos; no se reabren automáticamente omisiones históricas.

La omisión automática de archivos MEGA permanece deshabilitada hasta validar una firma individual de la versión MEGAcmd instalada. Por ahora los diagnósticos no clasificados conservan `STOP`; el informe permite recuperación manual con confirmación del usuario.

Los horarios no cambian la disponibilidad de la red. Para copiar desde un SMB, el host/contenedor del worker debe poder alcanzar el NAS al ejecutarse. Cloud Transfer Manager no levanta ni administra la VPN.

## Operación

El despliegue Compose incluye `beat`, `control-worker` y `evaluation-worker`, sin puertos publicados. `beat` solicita revisar las colas cada 15 segundos; `control-worker` reserva los conteos en PostgreSQL y los publica en `evaluations`. El `evaluation-worker` corre con concurrencia uno, descifra credenciales de proveedores y hace únicamente inventario de solo lectura. Su salud se consulta en `/health/evaluations`; su ausencia no declara caída una copia ya en curso. Mantén una sola instancia de Beat por despliegue.

El scheduler calcula la admisión usando `TRANSFER_MAX_CONCURRENT`, que debe coincidir con la concurrencia del worker `transfers` configurado en Compose. Los trabajos futuros y sus motivos permanecen en PostgreSQL durante reinicios. Si la red o las credenciales cambiaron, el trabajo queda en espera/bloqueado con un motivo visible; las credenciales se vuelven a validar justo antes de iniciar la ejecución.

## Despliegue de la evaluación anticipada

1. Respaldar PostgreSQL y abrir una ventana sin nuevas admisiones. Pausar Beat y `control-worker`, y esperar a que terminen las copias ya admitidas.
2. Aplicar las migraciones pendientes en orden, incluida `20261017_0013` para evaluaciones y `20261018_0015` para recuperación por archivo; comprobar el head esperado y el respaldo antes de reanudar admisiones.
3. Desplegar API y los workers `transfers`, `control` y `evaluations` con la misma versión, manteniendo Beat detenido. Comprobar `/health/ready` y `/health/evaluations`.
4. Iniciar una sola instancia de Beat y reanudar el despacho. Revisar una programación de prueba y confirmar que su conteo aparece en Programaciones antes de la hora de ejecución.

Si se revierte la aplicación, conservar la tabla y columnas de evaluación. No ejecutar downgrade mientras existan evaluaciones en uso. No se requiere reiniciar PostgreSQL ni Redis para este cambio.
