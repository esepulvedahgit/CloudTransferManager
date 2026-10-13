# Estado de implementación

**Revisión del repositorio: 2026-10-13.** Este documento resume lo que existe en el código y separa esa evidencia de la validación que depende de CI, credenciales o redes de una instalación.

## Estado por área

| Área | Estado en el repositorio | Evidencia y límites |
|---|---|---|
| Autenticación y usuarios | Implementado | Sesiones HttpOnly/SameSite con CSRF, Argon2id, bloqueo tras cinco intentos fallidos, roles `ADMIN`/`USER`, cambio obligatorio de contraseña y CLI inicial. Véanse `backend/app/main.py`, `backend/app/security/` y `backend/tests/test_api_security.py`, `test_user_management.py`, `test_cli.py`. |
| Conexiones y credenciales | Implementado | Administración, asignación privada, OAuth de Google, cifrado AES-GCM y respuestas públicas sin credenciales. El archivado es lógico y rechaza conexiones con trabajos activos. Véanse `backend/app/main.py`, `backend/app/security/credentials.py` y las migraciones. |
| Proveedores | Implementado con validación externa | MEGA usa MEGAcmd; Google Drive usa Rclone/OAuth; Seafile ofrece adaptadores Rclone o API explícito; la UI usa SMB/NAS como origen; Nextcloud usa Rclone WebDAV tras habilitación y allowlist de origen HTTPS. Cada instalación necesita sus credenciales, permisos y rutas de red. La compatibilidad de Seafile API se acota inicialmente a CE 13.x. Nextcloud requiere prueba real por instalación antes de activarlo. |
| Explorador | Implementado | Navegación, búsqueda, selección múltiple y destino por carpeta existente; paneles coordinados en escritorio y pasos separados en pantallas estrechas. No hay creación manual de carpetas desde la UI. Véanse `frontend/src/App.tsx` y `docs/specs/03-file-explorer-spec.md`. |
| Transferencias | Implementado | Operación `COPY`, ejecución de fondo, límites, cancelación, reintentos clasificados, staging por archivo, verificación y diagnóstico sanitizado. La reanudación por manifiesto está disponible para transferencias SMB fallidas o canceladas; no es una capacidad general de todos los adaptadores. |
| Programaciones | Implementado | Ejecuciones futuras de una sola vez, zonas IANA, modos `AUTO`/`EXCLUSIVE`, dependencias y despacho durable vía PostgreSQL, Celery Beat y `control-worker`. No hay recurrencia. Detalle en `docs/integrations/transfer-scheduling.md`. |
| Progreso e historial | Implementado | SSE sobre Redis en despliegue, recuperación del estado en frontend, historial y resultados por archivo para manifiestos SMB. Véanse `docs/specs/05-realtime-telemetry-spec.md` y el contrato API. |
| Auditoría y SMTP | Implementado con límite por confirmar | Registro de eventos, sanitización, configuración/prueba SMTP desde Administración, secretos SMTP cifrados y tareas de notificación. La aplicación escribe auditoría de forma append-only; no se encontró una protección de inmutabilidad impuesta por trigger o permisos de PostgreSQL. |
| Despliegue y CI | Implementado | Compose incluye proxy, frontend, backend, workers, PostgreSQL y Redis; solo el proxy publica puertos. Hay Docker Secrets, `tmpfs` para secretos/configuración Rclone, staging temporal disk-backed y workflow de calidad/Trivy en `.github/workflows/ci-security.yml`. El resultado del workflow corresponde a cada commit y PR. |

## Capacidades añadidas después del alcance inicial

- Migración de carpetas desde SMB/NAS a MEGA, Google Drive o Seafile, con manifiesto por archivo y reanudación controlada.
- Seafile mediante Rclone o API directa seleccionada por conexión; no hay fallback silencioso entre adaptadores.
- Nextcloud mediante WebDAV/Rclone con límites conservadores de concurrencia, ancho de banda y timeout; deshabilitado por defecto. Véase `docs/integrations/nextcloud-compatibility.md`.
- Programación única, modos de admisión, dependencias y vista de trabajos en segundo plano.
- Administración de conexiones privadas y archivado lógico sin borrar referencias históricas.
- Configuración SMTP desde Administración y menú de cuenta que agrupa administración, cambio de contraseña y cierre de sesión.
- CI de backend/frontend y análisis Trivy de repositorio, configuración e imágenes.

Las guías específicas están enlazadas desde [Documentación relacionada](../README.md#documentación-relacionada). Las restricciones de despliegue y los directorios temporales se describen en el [README](../README.md#temporales-secretos-y-datos-persistentes).

## Criterios de aceptación originales

La siguiente agrupación ayuda a estimar el avance, pero separa implementación de evidencia reciente. La existencia de una prueba o workflow no significa que haya pasado en el commit actual.

| Criterios | Estado de implementación | Evidencia que aún depende de ejecución o entorno |
|---|---|---|
| 1–4: autenticación, alta de usuarios y roles | Implementados | Resultado actual de tests backend. |
| 5–8: MEGA, OAuth Google y cifrado | Implementados en código | Autorizar/probar los proveedores con credenciales reales y confirmar los secretos del despliegue. |
| 9: conectividad a MEGA y Google Drive | Endpoints y adaptadores implementados | Éxito real depende de credenciales, OAuth, permisos y red de la instalación. |
| 10–14: exploración y selección | Implementados | Validación visual del recorrido en los tamaños de pantalla objetivo. |
| 15–16: copia en ambos sentidos | Implementada con pruebas automatizadas de adaptadores/procesador | Copia real con cuentas de prueba no se verificó en esta revisión. |
| 17–22: SSE, continuidad, cancelación y errores seguros | Implementados | Resultado del pipeline actual y comprobación operativa del worker/Redis. |
| 23–24: notificaciones de correo | Implementadas y configurables | Entrega real requiere SMTP operativo y no se probó aquí. |
| 25–27: historial, auditoría de solicitud y Compose | Implementados en código/configuración | Inicio completo del stack y revisión de respaldos no se ejecutaron aquí. |
| 28: interfaz responsive | Adaptaciones responsive presentes | Falta registrar una revisión visual/manual por viewport. |
| 29: suite automatizada aprobada | Tests y gates CI configurados | Debe verificarse el resultado para el commit candidato. |
| 30: documentación de instalación y operación | Documentada en README, specs y guías | Mantener las guías alineadas cuando cambien los contratos o la configuración. |

## Qué falta para declarar la entrega validada

La implementación cubre el flujo principal y varias extensiones que no estaban en el alcance inicial. No se puede afirmar un porcentaje exacto de aceptación: la lista de criterios aún no tiene evidencia de aprobación por criterio y el estado del pipeline cambia con cada commit.

Antes de declarar una versión lista para una instalación concreta, falta registrar evidencia de:

1. Todos los jobs de CI verdes para el commit candidato (`ruff`, tests y `mypy`; tests, lint y build del frontend; Trivy y builds Docker).
2. Conectividad y copia real con credenciales de Google Drive y MEGA; para extensiones habilitadas, validar además Seafile CE 13.x, Nextcloud y NAS desde las redes del backend/worker.
3. Revisión visual responsive en los tamaños soportados y recorrido de los flujos administrativos.
4. Decisión explícita sobre dos requisitos de hardening que siguen en la guía operativa: rate limiting de API e inmutabilidad de auditoría impuesta por la base de datos. No se encontraron implementaciones equivalentes en el árbol revisado.
5. Si SMB debe ser estrictamente solo origen, validar ese límite también en `POST /api/v1/transfers`: la UI filtra SMB del selector de destino, pero el endpoint no comprueba el tipo de proveedor destino.
6. Registrar la cobertura de los 30 criterios de aceptación como implementada, verificada en CI, validada contra proveedor real o pendiente, sin mezclar esas categorías.

Esta revisión fue documental y de lectura del código. No ejecutó la suite ni hizo conexiones a proveedores reales; por eso no afirma que el último PR o una instalación específica hayan pasado esas comprobaciones.

## Fases del plan original

| Fase | Estado en el código | Nota |
|---|---|---|
| 1. Base del proyecto | Implementada | Monorepo, servicios, persistencia, lint y pruebas están presentes. |
| 2. Vertical slice con mocks | Implementada | El flujo mock sigue como soporte de desarrollo y pruebas. |
| 3. Autenticación, roles y CLI | Implementada | Incluye bloqueo por intentos y cambio inicial de contraseña. |
| 4. Proveedores y cifrado | Implementada | Se añadieron MEGAcmd, Seafile y SMB además de Google Drive. |
| 5. Explorador | Implementada | El diseño actual está descrito en la spec 03. |
| 6. Transferencias reales | Implementada | Incluye staging y manifiesto SMB, más programación durable. |
| 7. SSE y notificaciones | Implementada | Incluye progreso, recuperación del estado y administración SMTP. |
| 8. Auditoría y hardening | Parcial | La auditoría y sanitización existen; rate limiting, garantía de inmutabilidad en BD si se mantiene como requisito y evidencia final de aceptación requieren cierre. |
