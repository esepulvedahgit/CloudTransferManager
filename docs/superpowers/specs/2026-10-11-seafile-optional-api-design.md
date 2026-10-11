# Diseño: proveedor Seafile con adaptador API opcional

## Objetivo

Agregar Seafile Community Edition 13.x como proveedor de origen y destino. Cada conexión usa rclone por defecto; un administrador puede seleccionar explícitamente el adaptador de API de Seafile para esa conexión. El sistema nunca cambia de adaptador automáticamente al fallar una transferencia.

## Estado actual y restricciones

- Las conexiones disponibles son `MOCK`, `MEGA` y `GOOGLE_DRIVE`; el modelo SQL tiene una restricción para esos valores.
- `StorageProvider` implementa prueba, listado, metadatos y cuota. El ejecutor de transferencias usa `RcloneRunner` para las conexiones compatibles y staging privado por archivo para MEGAcmd y copias mixtas.
- Las credenciales de proveedor se cifran con AES-256-GCM ligadas al UUID de la conexión. Las respuestas públicas solo indican si están configuradas.
- Rclone solo puede recibir los comandos `lsjson`, `copy`, `about` y `size`; toda operación de producto es `COPY`. No se puede exponer RC ni `rcd`.
- Seafile 13.x es la única rama CE que se declarará soportada inicialmente. Otras ramas quedan sin validar hasta que se añadan pruebas específicas.

## Decisiones de diseño

### Selección del adaptador

Se agrega `SEAFILE` como `provider_type` y un campo `adapter_type` con valores `RCLONE` y `SEAFILE_API`. El valor predeterminado es `RCLONE`; para otros proveedores el campo permanece en `RCLONE` y no altera su comportamiento. La selección se guarda en la conexión, la administra un usuario `ADMIN` y se devuelve como metadato no secreto.

La selección gobierna prueba de conexión, navegación y transferencias. Si se elige `SEAFILE_API`, esa conexión usa API en todas esas rutas. Un error de API falla o requiere reautenticación; nunca activa rclone como fallback implícito. El administrador puede cambiar el adaptador explícitamente.

### Credenciales y autenticación

Ambos adaptadores reutilizan la URL base, usuario y contraseña de la conexión, cifrados en el payload existente. Se permite elegir una biblioteca y configurar su contraseña cuando sea cifrada. El OTP de 2FA se acepta únicamente en el flujo de autenticación/prueba, no se persiste. El token de acceso resultante se guarda cifrado para los workers; si deja de ser válido, la conexión requiere reautenticación administrativa con un OTP nuevo. Nunca se guarda una semilla TOTP.

La UI no devuelve ni muestra contraseñas, OTP ni tokens. Las solicitudes mutables mantienen autenticación de administrador y CSRF. Los datos sensibles no aparecen en auditoría, errores ni logs.

### Adaptadores y transferencias

- `SeafileRcloneProvider` implementa navegación y cuota mediante `RcloneRunner`, con configuración de remoto efímera por operación y la lista blanca existente.
- `SeafileApiProvider` implementa la misma navegación y añade capacidades de transferencia de archivo: descarga por streaming y carga reanudable de Seafile. La API nunca ofrece acciones destructivas.
- Las conexiones rclone compatibles mantienen la copia remota directa. Cuando una transferencia incluye un extremo API, el worker transfiere un archivo a la vez mediante el staging privado actual, conserva rutas anidadas y limpia los temporales ante éxito, fallo o cancelación.
- Toda carga cuyo destino esté configurado con API calcula SHA-256 del archivo fuente y compara ese valor con una lectura de vuelta del destino antes de marcar el archivo completado. Una discrepancia falla el archivo con código saneado; no intenta automáticamente otro adaptador.
- Los reintentos de la API respetan cancelación, límites y códigos transitorios. Se reutiliza la identidad de transferencia y se reanuda la carga cuando el protocolo de Seafile lo permita; no se declara `COMPLETED` antes de completar y verificar el archivo.

## Contrato de conexión

`POST /api/v1/connections` acepta `provider_type: "SEAFILE"`, `adapter_type: "RCLONE" | "SEAFILE_API"` (opcional, predeterminado `RCLONE`) y credenciales con URL, usuario y contraseña. Se aceptan opciones de 2FA, biblioteca y contraseña de biblioteca. Un OTP, cuando se requiera, se envía en un campo transitorio a la autenticación/prueba y se elimina antes de cifrar credenciales.

`PATCH /api/v1/connections/{id}` permite cambiar `adapter_type` o actualizar las credenciales. `POST /api/v1/connections/{id}/test` permite autenticación inicial o reautenticación con OTP transitorio. Las respuestas contienen `adapter_type` y `credentials_configured`, nunca credenciales o tokens. Al cambiar credenciales o adaptador, se invalida el resultado de prueba previo y la conexión queda inactiva hasta una prueba exitosa.

Las conexiones existentes migran sin cambios de comportamiento y reciben `adapter_type = "RCLONE"`. Se agrega `SEAFILE` a la restricción de proveedores SQL. No se modifican transferencias existentes.

## Criterios de aceptación

1. Un administrador crea y prueba conexiones Seafile CE 13.x con `RCLONE` por defecto o seleccionando explícitamente `SEAFILE_API`.
2. Ambas selecciones permiten navegar bibliotecas y transferir archivos como origen o destino; las bibliotecas cifradas admiten la contraseña de biblioteca.
3. La ruta API usa carga reanudable, respeta cancelaciones y retries y solo marca completo después de verificar SHA-256 con lectura de vuelta.
4. La autenticación 2FA usa OTP transitorio. La expiración del token requiere reautorización y nunca provoca cambio automático a rclone.
5. La migración conserva las conexiones existentes; ninguna respuesta, auditoría o log expone secretos.
6. Rclone se invoca sin shell, únicamente con comandos permitidos y configuración temporal privada. La funcionalidad no agrega comandos remotos destructivos.
7. Pruebas unitarias, de API, persistencia y ejecutor cubren los dos adaptadores; pruebas de integración reproducibles validan Seafile CE 13.x con ambos modos.

## Fuera de alcance

- Compatibilidad declarada para Seafile 10, 11, 12 o 14.
- Fallback automático entre adaptadores, selección de motor por trabajo, sincronización, movimiento o eliminación remota.
- SSO/OAuth de Seafile, WebDAV y configuración de `rclone rc`/`rcd`.
- Verificación SHA-256 con lectura de vuelta para cargas que no tengan como destino una conexión API.
