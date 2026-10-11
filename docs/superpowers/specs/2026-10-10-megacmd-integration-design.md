# Diseño: integración MEGAcmd y staging temporal por archivo

## Objetivo

Usar el binario oficial MEGAcmd para las operaciones de MEGA y mantener Rclone para Google Drive. Las transferencias entre ambos proveedores se harán como `COPY`, usando staging temporal local de un archivo a la vez y retirándolo inmediatamente después de su copia.

## Contexto y restricciones

- El adaptador actual configura MEGA como backend de Rclone, pero la imagen de Rclone desplegada no incluye ese backend.
- Las credenciales de MEGA y Google Drive seguirán cifradas en PostgreSQL con el mecanismo existente. El frontend no recibirá credenciales.
- No se ejecutará ningún comando con shell ni se interpolarán rutas del usuario en un shell.
- Las transferencias no alterarán ni borrarán el origen. No se invocarán comandos MEGAcmd remotos destructivos (`rm`, `mv`, `sync`, `deleteversions`, etc.).
- MEGAcmd administrará listado, comprobación de conexión, cuota y operaciones MEGA; Rclone seguirá administrando Google Drive.

## Arquitectura propuesta

Un `MegaCmdRunner` ejecutará comandos mediante `asyncio.create_subprocess_exec` con argumentos separados, entorno aislado y captura de salida limitada/saneada. Solo permitirá los subcomandos de consulta/listado, descarga, subida y cierre de sesión necesarios para el MVP. El login se enviará por `stdin` a `mega-cmd`; la contraseña no se incluirá en los argumentos visibles del proceso ni en logs. Los comandos de archivos usarán los clientes `mega-*` permitidos con rutas como argumentos separados. Cada operación de trabajo tendrá un `HOME` y caché MEGAcmd privados, creados bajo el directorio temporal del worker, para evitar mezclar sesiones entre conexiones o trabajos.

El `MegaCmdProvider` implementará `StorageProvider` para prueba de conexión, navegación y cuota. `TransferExecutor` seleccionará el flujo según proveedor:

- MEGA → Google Drive: enumerar archivos hoja, descargar uno localmente con MEGAcmd, copiarlo a Drive con Rclone y borrar el staging antes de avanzar al siguiente archivo.
- Google Drive → MEGA: copiar un archivo de Drive a staging con Rclone, subirlo con MEGAcmd y borrar el staging antes de avanzar.
- Las carpetas se recorrerán recursivamente; no se pedirá a `mega-get` descargar una carpeta entera.

El staging estará en un directorio temporal del worker, no en un volumen persistente, y será configurable. Antes de cada descarga se comprobará el espacio disponible para el tamaño conocido del archivo. Esto evita el límite actual de 64 MB del `tmpfs` y no reserva RAM para archivos grandes. El número de archivos locales retenidos por cada trabajo será como máximo uno.

## Ciclo de vida y errores

La descarga, copia/subida y borrado local de cada archivo estarán encerrados en un `try/finally`. El staging se borra en éxito, fallo o cancelación; un error de borrado se propaga y detiene el trabajo antes del siguiente archivo. Al finalizar el trabajo también se cierra la sesión MEGAcmd y se elimina su `HOME`/caché temporal. El HOME solo se borra tras confirmar `mega-quit`; si no se confirma el cierre del daemon, se conserva el HOME y el trabajo falla. La limpieza de sesión se completa antes de propagar una cancelación del worker.

Las fallas de autenticación, comandos, espacio insuficiente o ausencia del elemento se convertirán en errores internos saneados; nunca se registrarán contraseñas, tokens ni la salida que pudiera contenerlos. La salida truncada de `mega-ls` causa un error explícito para impedir que se interprete un listado incompleto como completo. Los errores transitorios clasificados de descarga y copia Drive se reintentan con backoff y staging nuevo. Una subida MEGA fallida no se reintenta: su resultado remoto puede ser ambiguo, incluso si el listado inmediato no encuentra el elemento; se registra `MEGA_DESTINATION_STATE_UNCERTAIN` para evitar duplicados y borrados remotos.

Si el elemento ya existe en el destino MEGA, se omitirá y se reportará una advertencia; no se sobrescribirá ni eliminará el elemento remoto como parte de esta integración.

## Verificación

- Pruebas unitarias para la lista permitida de comandos, argumentos sin shell, ausencia de credenciales en argv y salida, aislamiento del `HOME` y limpieza del staging ante éxito, excepción y cancelación.
- Pruebas de proveedor para autenticación, listado, cuota, rutas y errores saneados con ejecutor MEGAcmd simulado.
- Pruebas del ejecutor para ambas direcciones, carpetas recursivas, omisión de conflictos y evidencia de que cada archivo temporal ya no existe antes de procesar el siguiente.
- Pruebas de infraestructura para instalación del binario oficial y configuración del directorio temporal en backend/worker.
- La validación real contra una cuenta MEGA requerirá credenciales de prueba suministradas fuera del repositorio.
