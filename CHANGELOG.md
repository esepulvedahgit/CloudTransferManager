# Historial de cambios

Este archivo registra las funciones y correcciones visibles para las personas que usan Cloud Transfer Manager. Añade los cambios nuevos en **[Sin publicar]**, agrupados por `Añadido`, `Cambiado`, `Corregido` o `Seguridad`. Al preparar una versión, mueve las entradas a su sección numerada y agrega la fecha de publicación.

## [Sin publicar]

<!-- Añade aquí los cambios que todavía no pertenecen a una versión publicada. -->

## [1.0.0] - 2026-10-19

### Añadido

- Primera versión estable de Cloud Transfer Manager.
- Inicio de sesión, roles de administrador y usuario, cambio obligatorio de contraseña inicial y administración de conexiones.
- Conexiones con MEGA, Google Drive, Seafile, Nextcloud y SMB/NAS; las credenciales de proveedores se cifran en el backend.
- Exploración de archivos y transferencias de copia con progreso en vivo, historial, reintentos y controles de cancelación, pausa o reanudación según el proveedor.
- Programación de transferencias de una sola ejecución, informes por archivo y gestión de incidentes.
- Staging de disco para los flujos que lo requieren, con comprobación de espacio libre y aviso en el informe de transferencias fallidas.
- Panel de métricas, notificaciones SMTP y despliegue con Docker Compose, PostgreSQL, Redis y workers Celery.
