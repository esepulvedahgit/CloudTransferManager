# Especificación 00: Visión General y Alcance del MVP

## 1. Contexto y Objetivos del Producto
**Cloud Transfer Manager (CTM)** es una aplicación web multiusuario privada, desplegada mediante Docker Compose y pensada para una red controlada por la organización. El reverse proxy publica la interfaz; PostgreSQL, Redis, API y workers permanecen en redes internas. El acceso desde una LAN depende también del firewall y del DNS/TLS administrados por la instalación.

Su objetivo primordial es proporcionar una interfaz visual y un motor robusto para explorar y transferir archivos entre servicios de almacenamiento en la nube, usando **Rclone** como motor interno aislado.

### Caso de Uso Base y Alcance Actual
El caso inicial era transferir archivos y carpetas bidireccionalmente entre **MEGA** y **Google Drive** mediante `COPY`. La aplicación actual conserva ese flujo y amplía el alcance: admite **Seafile** mediante Rclone o su adaptador API opcional, y permite explorar recursos **SMB/NAS como origen** y copiarlos a MEGA, Google Drive o Seafile.

El detalle de qué está implementado y qué necesita validación operacional se mantiene en [Estado de implementación](../implementation-status.md). Las especificaciones describen contratos y restricciones; no son por sí solas evidencia de que una ejecución de CI o una prueba contra un proveedor externo haya pasado.

---

## 2. Alcance del MVP vs Fuera de Alcance

### Alcance base y funcionalidades implementadas
1. Autenticación local mediante sesiones seguras (cookies HttpOnly/SameSite/CSRF).
2. Cuenta de administrador inicial creada por comando CLI seguro (`python -m app.cli create-admin`).
3. Administración de usuarios (CRUD, activación/desactivación, restablecimiento de clave).
4. Dos roles: `ADMIN` y `USER`.
5. Gestión de conexiones para: **Google Drive** (OAuth) y **MEGA** (credenciales cifradas).
6. Prueba de conectividad interactiva y consulta de cuota por conexión.
7. Explorador de archivos visual con soporte para archivos y carpetas.
8. Transferencias bidireccionales entre MEGA y Google Drive mediante operación `COPY`.
9. Cola de trabajos asíncronos en segundo plano (Celery + Redis).
10. Progreso y telemetría en tiempo real mediante Server-Sent Events (SSE).
11. Historial de transferencias con métricas completas (bytes, archivos, velocidad, ETA).
12. Cancelación manual de transferencias activas.
13. Registro inmutable de eventos de transferencia y eventos de auditoría.
14. Notificaciones por correo electrónico (SMTP) en segundo plano ante finalización o error.
15. Panel web responsive con diseño enterprise (React + MUI).
16. Despliegue completo y seguro mediante Docker Compose y Docker Secrets.
17. Cifrado en reposo para credenciales sensibles (AES-256-GCM / Envelope con Master Key externa).
18. Programación única de copias con zona horaria, modo automático/exclusivo y dependencias opcionales entre trabajos.

Las capacidades que se añadieron después del alcance inicial se detallan en las especificaciones e integraciones correspondientes: SMB/NAS, Seafile, archivado lógico de conexiones, configuración SMTP desde Administración, programación durable y su indicador de trabajos en segundo plano. No se debe interpretar esta lista como un resultado de certificación: la [matriz de estado](../implementation-status.md) separa implementación en el repositorio de comprobación en un despliegue real.

### Fuera del Alcance actual
- Proveedores cloud adicionales: Amazon S3, Wasabi, MEGA S4, Azure Blob, OneDrive y Backblaze B2.
- Programaciones recurrentes (CRON); solo hay ejecuciones únicas.
- Sincronización bidireccional (`sync`).
- Eliminación automática de archivos en origen (`move`).
- Límites de ancho de banda o cuotas por usuario.
- Autenticación federada (LDAP, Active Directory, OIDC).
- Workers distribuidos / Alta disponibilidad multi-nodo.
- Antivirus, escaneo de malware y deduplicación avanzada.

---

## 3. Restricciones e Invariantes Críticos del Sistema

1. **Entorno Privado:**
   - La aplicación está diseñada para operar en red privada/intranet.
   - Los servicios PostgreSQL, Redis, Workers y Rclone no deben publicar puertos en el host exterior; solo se comunican dentro de la red interna de Docker.
2. **Operación Exclusiva `COPY`:**
   - En el MVP, las transferencias solo copian. **NUNCA** se eliminan ni alteran archivos en el proveedor de origen.
   - Operaciones destructivas (`delete`, `purge`, `sync`, `move`) están deshabilitadas a nivel de código y bloqueadas por lista blanca.
3. **Sin persistencia de contenido de transferencia:**
   - Los archivos pueden pasar por staging local por archivo en `/var/lib/cloud-transfer/staging` cuando el adaptador lo requiere. Ese directorio es disk-backed, no es un volumen persistente ni `tmpfs`, y se limpia antes de continuar con el siguiente archivo.
   - Las configuraciones efímeras de Rclone se guardan en `/var/lib/cloud-transfer/tmp`, montado como `tmpfs`, con modo `0600`; se eliminan tras éxito, fallo o cancelación.
   - Ver [Despliegue](../../README.md#temporales-secretos-y-datos-persistentes) para capacidad, rutas y operación de los temporales.
4. **Terminología Adaptada por Proveedor:**
   - **Google Drive:** Unidades, Unidades Compartidas, Carpetas y Archivos.
   - **MEGA Tradicional:** Carpetas y Archivos (NO presentar como buckets).
   - **S3 / Wasabi / MEGA S4 (Futuro):** Buckets, Prefijos y Objetos.

---

## 4. Los 30 Criterios de Aceptación del alcance inicial

Estos criterios conservan la lista base del MVP. Su estado actual no se deduce del texto de requisito: véase el seguimiento separado en [`docs/implementation-status.md`](../implementation-status.md), que distingue código presente, aprobación de CI y validación contra servicios reales.

1. El administrador puede iniciar sesión exitosamente con credenciales válidas.
2. El administrador puede crear nuevos usuarios desde el panel.
3. El usuario creado puede iniciar sesión con sus credenciales.
4. Un usuario con rol `USER` no puede acceder a endpoints ni pantallas administrativas.
5. El administrador puede configurar y guardar una conexión a MEGA.
6. El administrador puede configurar y autorizar una conexión a Google Drive (OAuth).
7. Las credenciales de proveedores almacenadas en PostgreSQL están cifradas y no son legibles en texto plano.
8. La clave maestra de cifrado no reside en la base de datos ni en la imagen Docker (se inyecta por Docker Secret).
9. La aplicación ejecuta pruebas de conectividad exitosas contra MEGA y Google Drive.
10. El usuario puede navegar por carpetas y listar archivos en MEGA.
11. El usuario puede navegar por carpetas y listar archivos en Google Drive (Mi Unidad y Unidades Compartidas).
12. El usuario puede seleccionar uno o varios archivos individuales para transferir.
13. El usuario puede seleccionar una carpeta completa como origen de transferencia.
14. El usuario puede seleccionar la carpeta de destino en el proveedor destino.
15. La aplicación copia exitosamente elementos de MEGA a Google Drive.
16. La aplicación copia exitosamente elementos de Google Drive a MEGA.
17. El navegador muestra progreso en tiempo real (porcentaje, velocidad, archivo actual) sin recargar la página vía SSE.
18. Cerrar o recargar el navegador no interrumpe ni cancela la transferencia en curso.
19. El usuario puede reconectarse a la aplicación y consultar el progreso del trabajo activo.
20. El usuario puede cancelar una transferencia activa; el worker detiene el proceso Rclone de inmediato.
21. Los errores de transferencia y conexión se presentan al usuario en lenguaje amigable y sin stack traces.
22. Los detalles técnicos de error se registran internamente con Correlation ID y sin exponer secretos.
23. Se despacha un correo electrónico al completar exitosamente una transferencia.
24. Se despacha un correo electrónico cuando una transferencia falla.
25. La transferencia finalizada queda registrada en el historial con métricas definitivas.
26. El log de auditoría registra la identidad del usuario que ordenó la transferencia.
27. Todo el sistema se levanta de forma reproducible y funcional mediante `docker compose up`.
28. La interfaz web es completamente responsive (Desktop >=1280px, Tablet 768-1279px, Móvil <768px).
29. Las pruebas automatizadas (unitarias, integración y seguridad) pasan con éxito.
30. El repositorio contiene documentación completa de instalación, variables de entorno y operación.

### Criterios adicionales de programación
- Una transferencia con fecha futura se conserva en PostgreSQL y se ejecuta una vez al llegar su hora, incluso si Beat estuvo detenido; las fechas vencidas se recuperan en el siguiente ciclo.
- `AUTO` utiliza `TRANSFER_MAX_CONCURRENT` y deja en paralelo copias compatibles. Las copias que usan staging (SMB, MEGA y Seafile API) comparten un único cupo de staging.
- `EXCLUSIVE` espera a que los trabajos ya admitidos terminen y actúa como barrera FIFO: los trabajos posteriores esperan hasta que la exclusiva finalice.
- Una dependencia comienza cuando el padre termina en `COMPLETED` o `COMPLETED_WITH_WARNINGS`. Si termina en `FAILED` o `CANCELLED`, el hijo queda bloqueado hasta que se cambie su dependencia, se cancele o el padre se reanude.
- Solo las transferencias pendientes y todavía no admitidas se pueden reprogramar o cancelar inmediatamente. Las ejecuciones admitidas se cancelan mediante el worker.
