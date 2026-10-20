# Especificación 08: Tablero de métricas

## Objetivo

Exponer una vista autenticada en `/metrics` con los indicadores históricos y operativos que la plataforma puede respaldar. La vista vive en la misma aplicación React que el explorador para conservar el borrador de transferencia al cambiar de sección.

## Acceso y privacidad

- `ADMIN` consulta todos los trabajos o filtra por usuario y puede ver el resumen de salud del sistema.
- La UI excluye siempre las conexiones `MOCK` y no ofrece un selector de datos de prueba. El parámetro interno `include_mock` del API conserva su contrato para desarrollo.
- `USER` solo consulta los trabajos que solicitó y no puede enviar un `user_id` diferente. Los filtros por conexión pasan por la autorización de conexiones ya existente.
- El API devuelve agregados; nunca devuelve rutas, nombres de archivos, credenciales, tokens de despacho ni mensajes crudos del proveedor.
- La caché del endpoint se valida después de aplicar rol/alcance, dura hasta 15 segundos y separa las llaves por usuario, rol y filtros.

## Filtros

- Periodos `today`, `7d`, `30d` (predeterminado), `90d` y `custom`.
- Rango `custom` inclusivo, máximo 366 días; se rechazan fechas invertidas o incompletas.
- Zona horaria IANA del navegador para los límites y agrupación diaria; `UTC` es el valor de reserva.
- Proveedor origen/destino, conexión origen/destino y usuario para ADMIN.
- El estado de trabajos activos es un snapshot actual y no se limita por el periodo; conserva el usuario y filtros de conexión/proveedor.
- Los controles mantienen un borrador y consultan al pulsar **Aplicar filtros**. **Restablecer** recupera los valores predeterminados. Se mantienen visibles durante cargas y errores.
- En pantallas estrechas, **Más filtros** despliega usuario, nubes y conexiones e indica cuántas condiciones adicionales están seleccionadas. Período y fechas permanecen directamente visibles.
- Las fechas personalizadas se validan junto a los campos antes de enviar la consulta. Los resultados conservan el rango aplicado hasta confirmar un borrador válido.

## Indicadores

- Transferencias por estado, terminadas correctamente, terminadas con advertencias, fallidas, canceladas, reintentos y tasa de éxito.
- Archivos únicos de manifiesto: copiados, fallidos, omitidos explícitamente, saltados históricos, pendientes y renombrados. Carpetas no cuentan como archivos; bytes se suman cuando el tamaño es conocido.
- Proveedor de origen, destino y combinación; actividad diaria por transferencias creadas y copias registradas.
- Rendimiento por descarga origen, hash origen, carga destino, verificación destino y limpieza local de staging: muestras, mediana, p95, bytes/s cuando la etapa procesa bytes, fallos e intervalos incompletos.
- Distribución por tamaño/extensión, códigos seguros de error, razones de espera, pausa, carga de la cola y velocidad actual reportada.
- Solo ADMIN: estado de base de datos, Redis, workers, Beat y colas; agregados de conexiones, outbox de notificaciones y evaluaciones.
- La tarjeta de advertencias cuenta `completed_with_warnings`, sin sumar categorías de archivos que pueden solaparse. La tasa de éxito aparece sin valor cuando no existen transferencias terminadas para su denominador.
- La actividad diaria completa los días sin actividad con cero. Los gráficos ofrecen una alternativa tabular; proveedores alterna archivos y volumen conocido, sin sumar las series de origen y destino.

## Definiciones temporales y cobertura

- La dimensión de creación usa `transfer_jobs.created_at`. La actividad de archivo y las etapas usan sus timestamps efectivos aunque el trabajo se haya creado fuera del rango.
- `copied_at` se asigna la primera vez que un elemento del manifiesto pasa a `COPIED`; reanudaciones no generan otra copia lógica. La migración no infiere fechas a partir de `updated_at`.
- Para copias históricas sin fecha exacta, el tablero las cuenta en totales por manifiesto, las excluye del gráfico diario y muestra la cobertura con/sin fecha.
- El éxito es `(COMPLETED + COMPLETED_WITH_WARNINGS) / (COMPLETED + COMPLETED_WITH_WARNINGS + FAILED)`. Cancelaciones se reportan separadas y no alteran ese denominador.
- `COPIED` es distinto de `FAILED`, `SKIPPED` histórico y una omisión explícita (`SKIPPED` con `omission_kind`). Un archivo se cuenta una vez aunque use varias etapas o staging.
- Una etapa que se corta sin cierre permanece `OPEN`, sin duración ni velocidad inventadas. Los fallos de persistencia de métricas son best-effort y no cambian el resultado de la copia.

## Persistencia y librerías

- `transfer_metric_spans` persiste intervalos de estado y mediciones de etapa. Cada fila usa UUID, intento y token de despacho; referencias a manifiesto/conexión son opcionales y se eliminan con `SET NULL`.
- `transfer_manifest_items.copied_at` conserva la hora del primer resultado COPIED. Ambos cambios se despliegan mediante Alembic.
- La UI utiliza MUI X Charts Community para líneas, barras y distribución, conservando MUI y TanStack Query del proyecto. El endpoint actualiza la vista cada 30 segundos y respeta el estado visible de la pestaña.
- El usuario puede pausar el refresco automático del tablero. Esta acción no pausa transferencias. La lectura pausada y las consultas fallidas no se presentan como una comprobación de salud vigente.
- La carga inicial usa esqueletos sin retirar filtros. Un fallo posterior conserva el último resultado y su hora, con reintento y salud sin verificar. Las peticiones usan AbortSignal y llaves separadas por usuario/rol.
- La presentación de salud considera también los consumidores de todas las colas devueltas. Los estados se traducen y combinan texto/color; los estados guardados de conexiones no equivalen a una nueva prueba contra el proveedor.
- Los gráficos se actualizan sin animación de datos. La preferencia de movimiento reducido se respeta en progreso y controles; tablas con scroll local conservan acceso por teclado.

## Límites de la primera versión

No se consultan cuotas de proveedores ni CPU/RAM del host: pueden producir carga adicional, requieren permisos externos o una política explícita de exportación. El estado no productivo de infraestructura es deliberadamente simulado. Las métricas exactas de rendimiento empiezan cuando se despliega esta versión; los registros antiguos no se reconstruyen.
