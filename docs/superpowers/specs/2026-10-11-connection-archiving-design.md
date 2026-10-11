# Diseño: administración y archivado de conexiones

## Objetivo

Dar a los administradores una sección visible para consultar las conexiones de almacenamiento y archivarlas de forma segura. Una conexión archivada deja de estar disponible para nuevas operaciones, sus credenciales guardadas se eliminan y las transferencias históricas permanecen intactas.

## Contexto y restricciones

- Actualmente la API permite crear, editar, probar y desactivar conexiones, pero no ofrece un endpoint para eliminarlas ni una vista dedicada para administrarlas.
- El explorador obtiene las conexiones mediante `GET /api/v1/connections`; los administradores también pueden asignar conexiones privadas desde el diálogo de administración.
- `transfer_jobs.source_connection_id` y `destination_connection_id` son referencias obligatorias a `storage_connections` sin `ON DELETE CASCADE`. Las transferencias históricas deben conservar esas referencias.
- Los permisos de usuarios sí tienen `ON DELETE CASCADE`, pero al archivar una conexión se revocarán explícitamente.
- Las credenciales permanecen cifradas en PostgreSQL mientras la conexión está disponible. Al archivarla, el ciphertext se borra; nunca se devuelve al frontend ni se registra.
- La operación solo cambia configuración interna de la aplicación. No invoca proveedores ni elimina archivos remotos.
- Solo ADMIN puede archivar conexiones. Los cambios mutables conservan la protección CSRF.

## Enfoque seleccionado

El endpoint `DELETE /api/v1/connections/{connection_id}` realizará un archivado lógico, no borrará la fila de `storage_connections`. Se agrega `deleted_at TIMESTAMP WITH TIME ZONE NULL` para distinguir una conexión archivada de los estados operativos existentes (`ACTIVE`, `INACTIVE`, `ERROR`).

El archivado, dentro de una transacción:

1. Bloquea la fila de conexión para serializar la operación con la creación de transferencias.
2. Rechaza la operación con `409 Conflict` si existe una transferencia asociada en `QUEUED`, `VALIDATING`, `RUNNING`, `RETRYING` o `CANCELLING`.
3. Establece `deleted_at`, elimina `encrypted_credentials` y revoca los permisos de usuario.
4. Agrega un evento de auditoría `CONNECTION_ARCHIVED`, sin credenciales ni secretos.

La creación de una transferencia toma los bloqueos de las conexiones de origen y destino en orden determinista, comprueba que sigan activas y no archivadas, y crea el trabajo dentro de la misma transacción. Así, el archivado y el alta de transferencias concurrentes quedan serializados: gana una sola operación; nunca se confirma un trabajo que apunte a una conexión que acaba de archivarse.

Las conexiones archivadas quedan excluidas de `GET /api/v1/connections` y no pueden utilizarse para navegar, probar conexión, iniciar OAuth, cambiar configuración, asignar permisos ni crear transferencias. Un ID inexistente o archivado devuelve `404` en las operaciones de conexión. Una conexión asociada a un trabajo activo devuelve `409` al intentar archivarla. Las transferencias ya terminadas mantienen sus UUID de conexión y eventos.

## Interfaz

Se agrega una pestaña **Conexiones**, visible solo para administradores, junto al explorador. La navegación móvil existente entre Origen y Destino permanece dentro de la pestaña Explorador.

La sección presenta un inventario adaptable:

- En escritorio, filas tabulares compactas.
- En móvil, tarjetas apiladas con la misma jerarquía y controles de tamaño táctil.
- Cada conexión muestra nombre, proveedor, estado operativo, alcance global/privado y resultado/fecha de la última prueba.
- El botón principal `Agregar conexión` vive en el encabezado de esta sección.
- La acción `Archivar` es secundaria y no usa solo un icono como nombre accesible.
- Estados de carga, error y lista vacía tienen mensajes explícitos.

Antes de archivar, un diálogo confirma el nombre y proveedor de la conexión e informa que las credenciales guardadas se borrarán, las transferencias históricas se conservarán y la conexión desaparecerá del explorador. El diálogo bloquea envíos duplicados. Si el servidor responde `409`, se mantiene abierto y muestra el motivo junto al botón. Al éxito, se invalida la lista de conexiones, se limpia cualquier selección que apunte a la conexión archivada y se muestra una confirmación breve.

La presentación sigue la guía de Emil: jerarquía clara, estados legibles sin depender solo del color, respuesta breve al pulsar, controles accesibles para teclado y tacto, y movimiento reducido con `prefers-reduced-motion`. El archivado no tendrá animaciones prolongadas.

## Datos y auditoría

- `StorageConnection` y `ConnectionRecord` agregan `deleted_at` nullable.
- La migración agrega la columna sin alterar filas existentes.
- Los métodos de almacenamiento filtran conexiones archivadas en listados y operaciones de uso.
- El evento `CONNECTION_ARCHIVED` se agrega al registro de auditoría inmutable; el metadata solo incluye datos no secretos, como proveedor y alcance.
- La fila conservada satisface las claves foráneas de transferencias y evita borrar el historial.
- No se ofrece restauración desde la interfaz: las credenciales se destruyen al archivar y una conexión de proveedor se debe configurar/autenticar nuevamente para volver a usarla.

## Pruebas y verificación

- Pruebas de API: ADMIN puede archivar con CSRF; USER y solicitudes sin CSRF no pueden; UUID inexistente da `404`; conexión con trabajo activo da `409`; conexión sin trabajos activos queda oculta, sin credenciales ni permisos; historial terminado y eventos permanecen.
- Pruebas de repositorio: archivado actualiza la fila, elimina concesiones y ciphertext, y conserva trabajos históricos en implementaciones de memoria y SQL.
- Prueba de concurrencia SQL: archivar una conexión se serializa con el alta de una transferencia; no queda transferencia nueva ligada a conexión archivada.
- Pruebas de rutas: una conexión archivada no puede navegar, probar, iniciar OAuth, recibir permisos ni usarse en una nueva transferencia.
- Pruebas de interfaz: inventario admin, vista adaptable representada por los mismos datos, confirmación, manejo de `409`, actualización de lista y limpieza de selectores.
- Ejecutar suite backend, Ruff y mypy; ejecutar suite frontend, lint y build.

## Criterios de aceptación

1. ADMIN puede abrir una sección dedicada y consultar las conexiones disponibles con sus metadatos operativos, sin ver secretos.
2. ADMIN puede archivar una conexión si no hay una transferencia activa vinculada a ella.
3. El servidor impide archivar conexiones mientras haya trabajos activos y comunica el conflicto sin cambiar la conexión.
4. Después del archivado, la conexión no aparece en selectores ni permite operaciones nuevas; sus credenciales y permisos ya no están disponibles.
5. Las transferencias completadas, sus eventos y las auditorías previas siguen consultables y conservan sus referencias.
6. El cambio es seguro frente a una creación concurrente de transferencia.
7. La sección y el diálogo funcionan con teclado, lectores de pantalla y pantallas móviles.

## Alternativa descartada

El borrado físico exigiría reestructurar las claves foráneas o copiar metadatos de las conexiones en cada transferencia antes de eliminar la fila. El archivado lógico cumple el requisito de retirar la conexión cuando no hay trabajos activos y conserva las asociaciones históricas existentes con menos riesgo para la auditoría.
