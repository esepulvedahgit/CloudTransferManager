# Corrección de transferencias y despliegue

Plan aprobado en conversación el 2026-10-16. Ejecución inline.

## Entregables
- [ ] Corregir columnas del listado MEGA, preservando nombres exactos y rechazando inventarios ambiguos; validar descarga privada por archivo.
- [ ] Resultados COPIED/FAILED_SAFE/STOPPED, continuidad ante errores individuales, corte tras tres fallos iguales consecutivos, reanudación segura y rechazo de manifiestos MEGA incompatibles.
- [ ] Diagnósticos por etapa y resúmenes copiados/fallidos/pendientes/numerados en REST, SSE y frontend.
- [ ] Pruebas de regresión, suite backend, Ruff/mypy, frontend tests/lint/build y revisión independiente.
- [ ] Respaldar BD y referencias de imágenes; drenar y recrear seis servicios con la versión validada; probar salud y copia real gradual.
- [ ] Crear una recuperación basada en las 14 carpetas de TRF-2026-000005 resueltas nuevamente por identificador. Conservar historial y directorios existentes; no reanudar manifiestos corruptos.

## Invariantes y aceptación
Solo COPY; origen intacto; KEEP_BOTH universal (_01, _02 incluso con contenido idéntico). Ninguna escritura ambigua se repite sin reconciliar SHA-256. No secretos en logs. Un error individual puede dejar continuar pero el resultado final es FAILED y el progreso menor a 100%. Reintentos de salida MEGA ausente limitados a uno adicional; errores sistémicos detienen el trabajo. Sin migración de BD. Comparar resultado real de archivo Docker Desktop Installer.exe (619050928 bytes), luego lote de 20 archivos y recuperación completa. No declarar recuperación completa hasta todos los archivos verificados.
