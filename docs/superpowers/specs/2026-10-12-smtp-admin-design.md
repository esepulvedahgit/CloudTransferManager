# Configuración SMTP desde Administración

## Contexto

Cloud Transfer Manager ya envía notificaciones de transferencia mediante SMTP en una tarea de Celery. Actualmente el backend y el worker leen host, puerto, usuario, TLS, remitente y archivo de contraseña desde variables de entorno. El diálogo de Administración existente gestiona usuarios, accesos a conexiones y auditoría, pero no ofrece ajustes SMTP.

El objetivo es que un administrador pueda configurar SMTP desde una subpestaña dentro de ese diálogo, probar la conexión antes de guardar y mantener las credenciales cifradas en reposo.

## Alternativas consideradas

1. **Ajustes globales persistidos en PostgreSQL, credenciales cifradas con la clave maestra.** La UI administra el mismo ajuste que consultan los workers. Los valores actuales de entorno siguen funcionando mientras no exista una configuración guardada. Esta opción satisface la edición, persistencia y prueba desde la aplicación.
2. **Mantener todos los valores en variables de entorno y Docker Secrets.** Conserva el despliegue actual, pero la aplicación no puede guardar cambios desde la pestaña; requiere modificar el entorno y reiniciar servicios. No satisface el flujo solicitado.

Se elige la primera opción. Reutiliza la clave maestra ya montada y el cifrado AES-256-GCM envelope del repositorio, y deja los valores de entorno como compatibilidad para instalaciones existentes.

## Interfaz de Administración

- Agregar una navegación por pestañas al diálogo actual: la vista existente permanece en una pestaña general y se añade **Correo SMTP**.
- El formulario SMTP contiene servidor, puerto, usuario, contraseña, remitente y una opción **Usar STARTTLS**. Los campos de contraseña y usuario no se rellenan con valores guardados; la UI muestra si hay credenciales configuradas y explica que dejarlos vacíos conserva las actuales.
- **Probar conexión** valida el borrador actual contra el servidor SMTP sin enviar un correo ni guardar cambios. Muestra estados accesibles de progreso, éxito y error.
- **Guardar configuración** persiste los campos del formulario y muestra confirmación. Los errores se presentan sin datos de conexión ni credenciales sensibles.
- La presentación usa componentes y espaciado MUI del diálogo existente, con adaptación a pantallas estrechas.

## API, persistencia y flujo de datos

- Añadir rutas autenticadas solo para ADMIN:
  - `GET /api/v1/settings/smtp`: devuelve los campos no secretos y un indicador `credentials_configured`; nunca devuelve usuario ni contraseña ya guardados.
  - `PUT /api/v1/settings/smtp`: actualiza la configuración. Credenciales omitidas o vacías conservan el valor cifrado vigente; la ruta no devuelve secretos.
  - `POST /api/v1/settings/smtp/test`: prueba el borrador recibido sin persistirlo ni enviar un mensaje. Si no se proveen credenciales en el borrador, usa las guardadas o las de entorno disponibles.
- Crear un registro global de configuración SMTP con clave primaria UUID v4 y una clave de ámbito única que garantice una sola configuración SMTP. Los campos no secretos se almacenan como columnas; usuario y contraseña se cifran juntos usando AES-256-GCM envelope y la clave maestra del sistema. Los datos de credenciales solo se descifran dentro del backend o worker que los necesita.
- El repositorio de producción ofrece lectura y guardado del registro; el almacén en memoria mantiene el mismo contrato para los perfiles de desarrollo existentes.
- Antes de que exista un registro guardado, la lectura y el envío conservan la configuración de entorno actual. Una vez guardada, esta configuración pasa a ser la fuente del worker para las notificaciones futuras.
- La prueba de conexión reutiliza negociación SMTP y STARTTLS, y autentica solo si se configuraron credenciales. Sus errores se convierten a mensajes genéricos; no se registran solicitudes, contraseñas, usuario completo ni respuestas SMTP sin sanitizar.
- Guardar ajustes registra un evento `SMTP_SETTINGS_UPDATED` en auditoría. Sus metadatos incluyen únicamente qué campos no secretos cambiaron y nunca incluyen credenciales.
- El envío de notificaciones seguirá en la tarea Celery actual. Una falla SMTP se mantiene aislada del estado final de la transferencia.

## Migración y compatibilidad

- Añadir una migración Alembic para la tabla de configuración SMTP; no se introduce una configuración por defecto con credenciales.
- No se migran automáticamente los secretos de entorno a PostgreSQL. Los entornos existentes continúan funcionando hasta que un ADMIN guarde valores desde la UI.
- Actualizar el contrato de API y la especificación de auditoría/notificaciones para documentar las nuevas rutas, cifrado, prueba y precedencia de configuración.

## Criterios de aceptación

1. Un ADMIN puede abrir la subpestaña SMTP dentro del diálogo de Administración y guardar configuración persistente.
2. Un USER no puede leer, probar ni actualizar estos ajustes.
3. El frontend no recibe credenciales guardadas y puede conservarlas al guardar otros campos.
4. La prueba de conexión usa los valores del formulario, no persiste el borrador y no envía correo.
5. PostgreSQL guarda usuario y contraseña cifrados; el worker puede usar la configuración guardada para notificaciones.
6. Sin configuración guardada, el envío actual basado en variables de entorno continúa funcionando.
7. Los cambios quedan auditados sin incluir secretos y la interfaz presenta estados de carga y resultado coherentes con el estilo existente.
