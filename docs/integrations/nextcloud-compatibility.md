# Integración Nextcloud por WebDAV

## Alcance

Nextcloud se conecta mediante el backend WebDAV de Rclone (`type = webdav`, `vendor = nextcloud`). Un administrador agrega desde la interfaz la URL base de la instancia, el nombre de usuario y una contraseña de aplicación. La aplicación construye la ruta DAV de archivos; se puede incluir un prefijo de instalación, por ejemplo `https://cloud.example.com/nextcloud`. La integración permite explorar, consultar cuota y copiar archivos; no implementa sincronización, eliminación ni modificación del origen.

La integración está disponible en el selector de proveedores sin configuración de despliegue. Solo los administradores pueden registrar conexiones. El servidor debe usar HTTPS; la URL no puede incluir credenciales, consultas, fragmentos ni segmentos de ruta relativos. La confianza del host sigue el mismo modelo que Seafile: el administrador decide qué servidor conectar, incluidos servidores autohospedados.

## Configuración del despliegue

Configura estas variables en `.env` (el archivo no debe incluirse en Git):

| Variable | Predeterminado | Descripción |
|---|---:|---|
| `NEXTCLOUD_MAX_CONCURRENT` | `1` | Máximo de transferencias activas que incluyen Nextcloud en el clúster. |
| `NEXTCLOUD_BANDWIDTH_LIMIT` | `8M` | Límite Rclone por trabajo Nextcloud; sintaxis entera seguida opcionalmente de `K`, `M` o `G`. |
| `NEXTCLOUD_CHUNK_SIZE` | `10Mi` | Tamaño de fragmentos de carga WebDAV. Valores permitidos: `10Mi`, `32Mi`, `64Mi`, `128Mi`, `256Mi`, `512Mi` o `1Gi`. |
| `NEXTCLOUD_JOB_TIMEOUT_SECONDS` | `43200` | Tiempo máximo de un trabajo que usa Nextcloud; permitido entre 60 y 86400 segundos. |

Los límites Rclone de Nextcloud fijan `--transfers 1`, `--checkers 2`, conexión de 10 segundos y tiempo de inactividad de 60 segundos. El límite de concurrencia se aplica en la admisión de trabajos, compartida por todos los workers. Al aumentar el número de trabajos simultáneos, vuelve a evaluarse el consumo agregado de memoria, conexiones y disco temporal.

## Crear y probar una conexión

1. En Nextcloud, crea una contraseña de aplicación para el usuario técnico. No reutilices la contraseña interactiva.
2. En la aplicación, agrega una conexión Nextcloud e introduce la URL base HTTPS, el usuario y la contraseña de aplicación. Para un servidor publicado en un subdirectorio, incluye ese prefijo en la URL base.
3. La contraseña de aplicación se cifra con el mecanismo de credenciales existente. Nunca se devuelve al frontend ni se escribe en logs; Rclone recibe únicamente configuración temporal con permisos `0600`.
4. Ejecuta **Probar conexión**. Una conexión solo debe activarse después de que el test liste la raíz en la instancia real.

La validación real de extremo a extremo debe usar un usuario y datos de prueba en una instancia no productiva. Comprueba listado y cuota, una copia de archivo pequeño y grande en ambos sentidos y la conservación de origen. El test automatizado de la aplicación no certifica disponibilidad, rendimiento, límites del servidor ni configuración de proxy de una instalación específica.

## Compatibilidad y límites conocidos

- Configura proxy, DNS y certificados del sistema para que backend y worker alcancen el mismo host guardado en la conexión.
- Los listados se almacenan en una caché en memoria de cada proceso durante un máximo de 30 segundos. La caché está limitada por tamaño, entradas y volumen total, y las actualizaciones de credenciales cambian su clave.
- Rclone proporciona listado, metadatos y consulta de cuota sobre WebDAV. La cuota depende de que el servidor Nextcloud exponga valores DAV válidos; si no los informa, la aplicación deja esos valores vacíos.
- WebDAV y proxies inversos pueden limitar el tamaño de carga, tiempo de petición o tamaño de chunk. Ajusta esos límites en Nextcloud/proxy y valida con los tamaños operativos esperados antes de producción.
- Rclone usa fragmentos de carga de 10 MiB por defecto. Nextcloud y su proxy deben admitir el tamaño elegido; ajusta `NEXTCLOUD_CHUNK_SIZE` solo tras medir cargas reales y confirmar los límites de la instalación.
- El worker no hace fallback automático a otro adaptador. Un error de autenticación, red o servidor falla la operación según la política existente de reintentos.
- No se habilita la integración en ninguna instalación hasta verificarla contra su servicio real.

## Revisiones para puesta en producción

Antes de habilitar en una instalación, registra versión de Nextcloud, versión de Rclone, prueba de conexión, resultados de copia por sentido y tamaño, ancho de banda observado y errores sanitizados. No registres contraseñas de aplicación, contenido de configuración Rclone ni URLs que contengan credenciales.

## Referencias técnicas

- [Backend WebDAV de Rclone](https://rclone.org/webdav/): configuración `type = webdav`, `vendor = nextcloud`, contraseña obscure y tamaño de fragmentos.
- [Overview de backends y capacidad `about` de Rclone](https://rclone.org/overview/): el backend WebDAV admite `about`, sujeto a lo que exponga el servidor.
- [Manual de usuario de Nextcloud](https://docs.nextcloud.com/server/latest/Nextcloud_User_Manual.pdf): uso de WebDAV y contraseñas de aplicación.
