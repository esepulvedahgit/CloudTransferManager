# Migrar carpetas desde un NAS SMB

Cloud Transfer Manager accede al NAS desde el backend para probar y explorar la conexión, y desde el worker para inventariar y copiar los archivos. El backend o worker debe tener conectividad TCP al NAS por la LAN o por una VPN ya administrada por la infraestructura. CTM no crea, configura ni mantiene esa VPN.

## Red

- Configure la ruta y el DNS del host Docker para alcanzar el NAS. Si la VPN corre en ese host, confirme que el tráfico de los contenedores hacia la subred remota use esa ruta.
- Permita desde el host/contenedores de CTM hacia los destinos SMB aprobados el puerto TCP 445. No publique el puerto SMB ni exponga PostgreSQL, Redis, backend o worker.
- Backend y worker comparten la red de salida `provider_egress`; ambos necesitan llegar al NAS y a los proveedores cloud. La red no crea una ruta hacia redes corporativas por sí sola: la ruta/firewall de la infraestructura debe permitirla.
- Para validar la configuración, pruebe la conexión SMB desde la interfaz antes de iniciar una migración. Una caída de ruta o autenticación debe fallar como una conexión no disponible; no desconecte la VPN durante una migración.

## Autorizar destinos NAS

En `.env`, defina la lista separada por comas `SMB_ALLOWED_TARGETS`. Use nombres DNS o IP exactos, sin protocolo ni ruta. Un valor vacío deniega conexiones SMB. Por ejemplo:

```dotenv
SMB_ALLOWED_TARGETS=nas01.intranet.local,10.50.0.20
SMB_INTERACTIVE_TIMEOUT_SECONDS=30
SMB_TRANSFER_MAX_FILES=100000
```

Recree backend y worker después de cambiar la configuración:

```bash
docker compose up -d --build backend worker
```

El campo **Host** al agregar el NAS debe coincidir con uno de los destinos autorizados. El formulario guarda host, puerto, recurso compartido, carpeta base opcional, dominio y usuario; la contraseña queda cifrada en PostgreSQL. El recurso compartido y la carpeta base forman la raíz lógica que verá el explorador como `/`.

## Validación funcional

La conexión se validó desde **Servicios privados** con estos valores de esta instalación:

- NAS: `192.168.1.11`, puerto TCP `445`.
- Recurso compartido: `STORAGE` (sin `/` inicial).
- Lista permitida: `SMB_ALLOWED_TARGETS=192.168.1.11`.
- Resultado: la prueba de conexión desde la aplicación fue exitosa.

Si `STORAGE` es una subcarpeta dentro de otro recurso compartido, configura el nombre del recurso compartido en ese campo y `STORAGE` como **Carpeta raíz**. Las credenciales se ingresan en la aplicación y no se incluyen en esta documentación.

## Flujo de migración

1. Un administrador agrega y prueba el NAS desde **Servicios privados**.
2. El usuario abre **Explorar NAS**, navega la raíz lógica y selecciona una o más carpetas o archivos.
3. En el explorador selecciona Google Drive, MEGA o Seafile como destino y define la carpeta cloud.
4. El worker inventaría las carpetas en lotes persistidos de 200 elementos y copia cada archivo con la operación Rclone permitida `copy`. El origen SMB solo se lee.
5. El historial muestra progreso y permite consultar el resultado por archivo. Una transferencia fallida o cancelada puede reanudarse mientras las versiones de credenciales y la raíz del NAS sigan iguales.

`SMB_INTERACTIVE_TIMEOUT_SECONDS` limita las consultas de conexión y exploración del NAS (1–300 segundos, por defecto 30). No limita la duración de copias grandes. `SMB_TRANSFER_MAX_FILES` limita el total por transferencia SMB y admite más de los 5.000 archivos del flujo cloud heredado. Las transferencias con MEGA o Seafile API usan el directorio temporal de staging para un archivo por vez; revise que `TRANSFER_STAGING_PATH` tenga espacio para el archivo más grande más la reserva configurada.

Rclone está fijado a la versión `1.75.2` en la imagen backend/worker, incluye el backend SMB y se descarga con validación SHA-256 del paquete. Las versiones futuras deben actualizarse junto con las pruebas de integración de NAS y proveedores cloud.
