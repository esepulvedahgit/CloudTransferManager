# Conservación de archivos mediante numeración en todas las transferencias

## Objetivo y autorización

El usuario aprobó conservar los archivos mediante nombres consecutivos `_01`, `_02`, etc., y solicitó aplicar la regla a todas las nubes y tipos de transferencia. El origen permanece intacto. Este documento registra el estándar y la implementación preparada en el repositorio.

Regla aclarada por el usuario: cualquier nombre ocupado en la misma ruta de destino genera una nueva copia numerada, sin comparar su contenido, incluso si es idéntico o se solicita una transferencia nueva de los mismos archivos. Un reintento o reanudación del mismo trabajo conserva su asignación anterior.

## Evidencia y situación actual

- `TRF-2026-000002` falló al 69,14 % descargando `History API.mp4`. MEGA lista dos objetos con ese nombre y tamaño de 28.443.843 bytes en la misma carpeta. Una descarga aislada reprodujo el código 12 y «already exists», sin subir a Seafile.
- `StorageItem` conserva ruta y nombre, pero no la identidad del objeto remoto. El inventario común descarta una segunda entrada con la misma ruta.
- El manifiesto persistente se utiliza actualmente para SMB y su unicidad también depende de la ruta de origen.
- Las rutas MEGA, Seafile API, Rclone directo y Drive → Nextcloud tienen comprobaciones y políticas de conflicto diferentes. MEGA destino omite existentes; las demás utilizan `SKIP` o `OVERWRITE_IF_DIFFERENT`.

## Estándar implementado en código

1. Las transferencias nuevas utilizan `KEEP_BOTH`: copian a un nombre disponible y conservan los objetos existentes. La interfaz presenta «Conservar ambos (numerar duplicados)» como comportamiento general.
2. El primer archivo utiliza su nombre original si está libre. Los siguientes utilizan `History API_01.mp4`, `History API_02.mp4`, etc. El contador tiene al menos dos dígitos y continúa después de 99. Si un candidato ya existe o está reservado, se busca el siguiente.
3. Coincidir en nombre es suficiente para numerar. No se compara el contenido de todos los existentes antes de copiar. La igualdad de tamaño no permite descartar un objeto.
4. Se conserva la extensión. Para archivos sin extensión y archivos ocultos sin extensión se agrega el sufijo al nombre completo. Se respeta el límite de bytes de cada componente del destino, acortando únicamente la base del nombre cuando sea necesario para alojar el sufijo y la extensión; si la extensión por sí sola impide un nombre válido, se informa el conflicto sin escribir.
5. Las claves de colisión se calculan con normalización Unicode NFC y comparación conservadora sin distinción de mayúsculas, para conservar ambos archivos también en destinos que equivalen esos nombres. El nombre visible sigue las reglas del proveedor.
6. Las carpetas existentes compatibles se reutilizan para conservar la estructura habitual. Dos carpetas distintas del origen que producirían la misma ruta relativa reciben asignaciones distintas; todo su subárbol utiliza la asignación elegida. Si una carpeta colisiona con un archivo existente, se numera la carpeta.
7. Se registran la ruta original, la ruta asignada y el motivo del renombrado. El historial permite consultar los archivos renombrados y la finalización informa las advertencias una sola vez por asignación.
8. La misma regla se aplica a archivos individuales, carpetas, selección múltiple, copias inmediatas, programadas, exclusivas, dependientes, reintentos y reanudaciones de los pares reales de proveedores admitidos. Incluye ambos adaptadores Seafile, Google Drive, MEGA, Nextcloud y SMB/NAS como origen donde ya está permitido. El proveedor mock sigue siendo una simulación de progreso y no representa escrituras ni colisiones reales.

No se habilitan proveedores, combinaciones o permisos que actualmente estén prohibidos. La numeración crea una copia con otro nombre en destino; no renombra, mueve ni elimina objetos del origen ni del destino.

## Identidad y lectura del origen

- El modelo interno incorpora `source_object_id` opcional y referencias de carpeta padre. En proveedores que admiten nombres repetidos, el inventario utiliza el identificador estable del proveedor; la ruta queda como información visible.
- MEGA conserva los handles de `mega-ls` y utiliza una referencia inequívoca en `mega-get`. El runner valida estrictamente tanto los handles como los argumentos de listado; no se agregan comandos remotos destructivos.
- Google Drive conserva los IDs de `lsjson`. Para los objetos ambiguos, la lectura se realiza por ID mediante una capacidad permitida y verificada del adaptador; si Rclone `copy` no permite seleccionar inequívocamente ese objeto, se utiliza una lectura HTTP de Drive por ID con el OAuth cifrado existente. Las carpetas también se recorren por identidad, evitando seleccionar arbitrariamente una carpeta homónima.
- Los demás proveedores utilizan su identificador cuando lo ofrecen; una ruta solo funciona como identidad cuando el proveedor garantiza su unicidad en ese contexto.
- La selección repetida o solapada del mismo objeto no lo inventaría dos veces. Dos objetos diferentes con el mismo nombre sí se conservan y se contabilizan por separado.

## Asignación y persistencia comunes

- Un servicio común determina el destino antes de escribir cada archivo y guarda una asignación durable: trabajo, conexión destino, identidad de origen, ruta solicitada, ruta asignada, estado, inicio de escritura y evidencia de verificación.
- Se extiende el manifiesto existente para todas las rutas, con identidad de origen y unicidad por objeto, evitando otra tabla de progreso que compita con él. La migración conserva los registros y resúmenes SMB existentes.
- Los nombres ocupados se obtienen mediante listado paginado y reutilizable por carpeta a medida que se necesita. Las reservas locales se agregan al mismo conjunto; no se ejecuta una búsqueda completa por cada sufijo.
- La reserva y su evento se persisten antes de enviar bytes al destino. Los trabajos que escriben en una conexión se serializan con los mecanismos de admisión existentes y una reserva transaccional para evitar que dos trabajos de la aplicación asignen el mismo candidato.
- Un reintento recupera la misma asignación. Un archivo ya copiado y verificado se conserva; un resultado ambiguo requiere reconciliación por contenido antes de continuar. No se cambia automáticamente a `_02` después de un timeout de `_01`.
- Una transferencia nueva tiene nuevas asignaciones y numera contra los nombres que ya están presentes, conforme a la regla aclarada por el usuario.

## Ejecución de la copia

- La decisión del nombre pertenece al servicio común; los proveedores reciben la identidad de lectura y la ruta exacta asignada.
- Las rutas que actualmente copian un árbol completo con Rclone pasan a ejecutar los archivos inventariados, para que Rclone no omita duplicados ni genere sus propios nombres fuera del manifiesto.
- Cuando Rclone solo permita copiar a una carpeta conservando el nombre local, se utiliza staging por archivo con el nombre asignado. Ese cambio debe reflejarse en la admisión y reserva de capacidad temporal.
- Se conservan los perfiles de Nextcloud, la exportación única de documentos Google y su verificación SHA-256, la verificación por contenido de Seafile API, la cancelación durable, la limpieza de temporales y el tratamiento seguro de una escritura MEGA ambigua.
- Se usan operaciones de creación sin reemplazo donde el proveedor las ofrezca. Se vuelve a comprobar el destino antes de escribir y se verifica la ruta realmente creada. Una colisión concurrente demostrada antes de escribir permite reservar otro candidato; una respuesta ambigua después de escribir exige reconciliación.
- Las reservas serializan trabajos de esta aplicación por conexión. No se promete exclusión atómica universal frente a escritores externos o conexiones distintas que representen la misma cuenta: los adaptadores deben documentar esa limitación y detener un resultado ambiguo sin sobrescritura intencional ni reintento ciego.

## Rendimiento

La detección de duplicados del origen se incorpora al inventario existente, sin otra lectura completa del árbol. El worker enumera todo el árbol antes de iniciar la copia para fijar el manifiesto y el progreso; por eso, con muchos archivos puede haber una espera inicial visible. El límite de SMB se conserva en `SMB_TRANSFER_MAX_FILES`, pero el inventario y las asignaciones se mantienen en memoria hasta insertarse al manifiesto en lotes de base de datos. Una mejora posterior puede asignar y copiar por carpeta o página si el tiempo de inicio o la memoria resultan altos en producción.

El POST sigue respondiendo sin esperar listados remotos. El inventario permanece en el worker. Las lecturas por ID, las reservas y la copia por archivo pueden añadir peticiones y staging frente a una copia recursiva directa; esa diferencia se mide y se informa, sin prometer un inicio instantáneo ni rendimiento idéntico.

## Compatibilidad y despliegue

- Se añade `KEEP_BOTH` al contrato REST y se convierte en el predeterminado de las transferencias nuevas, tanto inmediatas como programadas. El API rechaza otras políticas en solicitudes nuevas.
- Los registros históricos no cambian de significado. Los trabajos existentes conservan su política guardada; el tratamiento inequívoco de objetos duplicados del origen se incorpora a todas las rutas.
- `SKIP` y `OVERWRITE_IF_DIFFERENT` se aceptan al ejecutar trabajos históricos guardados, pero no se aceptan como políticas en solicitudes nuevas.
- Se actualizan las especificaciones 02/03/04, el contrato REST, las guías de SMB y programaciones y el estado de implementación. El manifiesto y los eventos no exponen credenciales ni URLs firmadas.
- La entrega se prepara y verifica en código antes del despliegue. Aplicar migraciones y reconstruir o reiniciar servicios de producción es un paso separado; no se reinician transferencias fallidas como parte de esta modificación.

## Validación requerida

- Reproducción del caso de dos objetos MEGA con nombre idéntico e identidades diferentes; ambos llegan al destino con nombres distintos y conteos correctos.
- Matriz de rutas: Rclone directo, MEGA como origen/destino, Seafile API como origen/destino, Drive → Nextcloud, SMB/NAS y mock, incluidas las copias dentro del mismo proveedor admitidas por la aplicación.
- Colisiones con original, `_01` y `_02` ya presentes; nombres Unicode, diferencias de mayúsculas, sin extensión, ocultos, extensiones largas y límites de bytes.
- Carpeta contra archivo, carpetas homónimas con IDs distintos, selección solapada y preservación de carpetas vacías.
- Reintento por archivo, reanudación del mismo trabajo, nueva transferencia del mismo origen, respuesta remota ambigua y dos trabajos que compiten por el mismo nombre.
- Cancelación y limpieza; integridad de exportaciones Google y cargas Seafile API; compatibilidad de manifiestos SMB y políticas históricas.
- Pruebas automatizadas aisladas y comprobaciones de lint/tipos/build apropiadas. Las pruebas de proveedores reales y cualquier escritura en almacenamiento requieren un entorno de prueba y autorización de ese alcance.

## Archivos principales modificados

`backend/app/providers/base.py`, adaptadores MEGA/Drive/Seafile/Nextcloud, runners seguros, nuevo servicio común de asignación, `transfer_executor.py`, ambos stores, modelos/migración de manifiesto, admisión de transferencias, contratos API, `frontend/src/App.tsx`, pruebas y documentación relacionadas.

## Estado de implementación y validación

La implementación está en el árbol de trabajo junto con la migración `20261016_0012`; la migración no se ha aplicado. No se han ejecutado transferencias ni se han modificado servicios de almacenamiento. La suite backend no está disponible en el entorno actual porque falta `pytest`; las herramientas de frontend existen, pero no pueden ejecutarse porque no hay runtime `node`. La validación con proveedores reales sigue pendiente.
