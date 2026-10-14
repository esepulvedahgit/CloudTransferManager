# Diagnóstico Google Drive → Nextcloud, 2026-10-14

## Evidencia

- Transferencia `TRF-2026-000040`: 105 archivos, 1.158.580.918 bytes de inventario, inicio 01:27:31 UTC y fallo 01:39:05 UTC.
- Primer evento al 99,99 %: 01:27:54 UTC, con solo 209.733 bytes reportados. El porcentaje de Rclone depende del inventario que descubre durante la copia; el código conservaba el máximo observado aunque el total creciera.
- La salida de copia retenía únicamente los primeros 64 KiB de stderr. Los errores terminales de trabajos largos podían quedar fuera de la captura, con lo que se perdía su clasificación y diagnóstico.
- Comparación de solo lectura: 93 archivos coincidían en tamaño, 10 exportaciones de documentos Google a DOCX tenían tamaño de origen `-1` y dos archivos no figuraban en sus rutas esperadas.
- La copia acotada del TXT faltante (383 bytes), usando `SKIP` en el destino original, reprodujo tres rechazos de Nextcloud: HTTP 400, `InvalidPath`, «El nombre del archivo es demasiado largo». El nombre ocupa 394 bytes UTF-8, 382 caracteres. No se renombró automáticamente.
- El PDF faltante (6.320.325 bytes) se descargó de Drive a staging temporal, se eliminó ese staging y se copió correctamente al destino original usando `SKIP`. No se alteró el origen.

## Correcciones aplicadas

- El progreso de copias directas usa los bytes del inventario previo como denominador fijo.
- La captura de copia conserva los últimos 64 KiB, con memoria acotada. Los eventos persisten únicamente estados HTTP y categorías de operación/error reconocidas; nunca stderr crudo ni URLs.
- El rechazo por nombre demasiado largo en Nextcloud tiene código `NEXTCLOUD_FILENAME_TOO_LONG` y mensaje específico.

## Limitaciones detectadas durante el diagnóstico inicial

- `files_completed` en la ruta directa contaba verificaciones posteriores al éxito del comando completo, por lo que podía seguir en cero durante una copia que ya escribió archivos en el destino.
- El tamaño `-1` de exportaciones Google significa desconocido. La validación lo aceptaba como entero y después intentaba compararlo contra el tamaño positivo del DOCX. Las diez diferencias observadas no demostraban corrupción.
- El rechazo por nombre largo está confirmado en una copia acotada; los logs antiguos no permiten atribuir retrospectivamente todos los errores originales al mismo motivo.

## Verificación

85 pruebas del runner Rclone, executor, telemetría y proveedores Nextcloud/Drive pasan. Ruff y mypy de los módulos modificados pasan. La prueba de regresión reproduce tanto el porcentaje prematuro como la pérdida del error terminal tras más de 64 KiB de salida.

## Corrección posterior de exportaciones y contador

Google Drive → Nextcloud ahora verifica cada archivo antes de actualizar el contador. Los tamaños negativos se normalizan a desconocidos. Una exportación se materializa una vez en staging con límite de 64 MiB (o el límite global menor), se suben esos mismos bytes y se compara SHA-256 con una lectura acotada de Nextcloud. Un destino existente idéntico se verifica y conserva. El staging se limpia en fallos y cancelaciones.

La primera prueba real `TRF-2026-000041` descubrió que Rclone informa `directory not found` cuando la carpeta destino aún no existe. El adaptador Nextcloud ahora devuelve `None` en ese caso, conservando errores de permisos; la regresión está cubierta.

126 pruebas de backend y 45 de frontend pasan, además de Ruff, mypy, lint y build. Incluyen corrupción del destino con tamaño idéntico, conservación de un exportado existente idéntico, límite de exportación, cancelación y limpieza, rechazo previo de nombres largos y contador intermedio por archivo. La UI muestra bytes transferidos cuando el total exportado sigue desconocido, sin presentar «de 0 B».

La prueba real posterior `TRF-2026-000042`, en `/CTM-verificacion-20261014-020526`, terminó `COMPLETED`: documento Google exportado de 8.838 bytes verificado por SHA-256 y archivo normal de 383 bytes. Los eventos persistidos registraron `RUNNING` con `1/2` y el cierre con `2/2`, 9.221 bytes y 100 %. Backend y frontend reconstruidos, servicios saludables y `/health/ready` responde 200.

La repetición `TRF-2026-000043` sobre la misma carpeta existente también terminó `COMPLETED`, con `1/2` durante la copia/verificación y `2/2` al cerrar. La prueba automatizada de destino exportado idéntico confirma cero subidas; la prueba real confirma que la verificación funciona también sobre destinos existentes. La carpeta completa original requiere acortar previamente el nombre del TXT rechazado; no se alteró el origen para hacerlo.
