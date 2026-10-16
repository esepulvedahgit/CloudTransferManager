# Programar transferencias

Cloud Transfer Manager admite copias programadas una sola vez. La fecha se introduce en la zona local del navegador y se guarda normalizada a UTC junto con el identificador de zona IANA. No hay recurrencia en esta versión.

## Crear una programación

1. En el explorador, selecciona los archivos o carpetas y el destino.
2. Elige **Programar transferencia**.
3. Indica fecha y hora futuras, el modo de ejecución y, de manera opcional, una transferencia padre.
4. Consulta el estado y motivo de espera en **Programaciones**. Desde ahí puedes reprogramar o cancelar los trabajos todavía pendientes.

La sección recorre todas las páginas de trabajos pendientes y excluye las transferencias ya finalizadas.

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

Una transferencia hija comienza después de que su padre termina en `COMPLETED` o `COMPLETED_WITH_WARNINGS`. Si el padre falla o se cancela, el hijo queda bloqueado por seguridad. Reprograma el hijo para elegir otro padre o cancélalo; si corresponde reanudar el padre, el dispatcher volverá a evaluar la dependencia.

Los horarios no cambian la disponibilidad de la red. Para copiar desde un SMB, el host/contenedor del worker debe poder alcanzar el NAS al ejecutarse. Cloud Transfer Manager no levanta ni administra la VPN.

## Operación

El despliegue Compose incluye `beat` y `control-worker`, sin puertos publicados y en la red interna. `beat` solicita revisar la cola cada 15 segundos; `control-worker` toma esos avisos y el dispatcher reserva los trabajos en PostgreSQL antes de publicarlos en la cola `transfers`. Mantén una sola instancia de Beat por despliegue.

El scheduler calcula la admisión usando `TRANSFER_MAX_CONCURRENT`, que debe coincidir con la concurrencia del worker `transfers` configurado en Compose. Los trabajos futuros y sus motivos permanecen en PostgreSQL durante reinicios. Si la red o las credenciales cambiaron, el trabajo queda en espera/bloqueado con un motivo visible; las credenciales se vuelven a validar justo antes de iniciar la ejecución.
