# Especificación 00: Visión General y Alcance del MVP

## 1. Contexto y Objetivos del Producto
**Cloud Transfer Manager (CTM)** es una aplicación web multiusuario privada, desplegada localmente mediante Docker y accesible exclusivamente desde la red interna de una organización.

Su objetivo primordial es proporcionar una interfaz visual y un motor robusto para explorar y transferir archivos entre servicios de almacenamiento en la nube, usando **Rclone** como motor interno aislado.

### Caso de Uso Primario del MVP:
> Transferir archivos y carpetas bidireccionalmente entre **MEGA** y **Google Drive** (`MEGA -> Google Drive` y `Google Drive -> MEGA`), estableciendo una arquitectura extensible para admitir posteriormente Wasabi, Amazon S3, MEGA S4 y Azure Blob Storage sin reescribir el núcleo.

---

## 2. Alcance del MVP vs Fuera de Alcance

### En el Alcance (MVP):
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

### Fuera del Alcance (Versiones Posteriores):
- Proveedores adicionales: Amazon S3, Wasabi, MEGA S4, Azure Blob, OneDrive, Backblaze B2.
- Programaciones recurrentes (CRON).
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
3. **Persistencia Cero de Archivos Locales:**
   - Las transferencias deben transmitirse por streaming o mediante almacenamiento temporal acotado (`/var/lib/cloud-transfer/tmp`).
   - El almacenamiento temporal debe auto-limpiarse inmediatamente al terminar o fallar el trabajo.
4. **Terminología Adaptada por Proveedor:**
   - **Google Drive:** Unidades, Unidades Compartidas, Carpetas y Archivos.
   - **MEGA Tradicional:** Carpetas y Archivos (NO presentar como buckets).
   - **S3 / Wasabi / MEGA S4 (Futuro):** Buckets, Prefijos y Objetos.

---

## 4. Los 30 Criterios de Aceptación del MVP

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
