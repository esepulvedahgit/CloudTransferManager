# Revisión de Métricas — 2026-10-18

## Alcance y criterios

Revisión del dashboard completo: lectura de cifras, filtros, peticiones, carga inicial, refresco, errores, gráficos, tablas, salud y pantallas estrechas. Cambios limitados al frontend de Métricas y su documentación, sobre el checkout `develop`.

Se consultaron e instalaron cinco skills del repositorio oficial [emilkowalski/skills](https://github.com/emilkowalski/skills):

- [emil-design-eng](https://github.com/emilkowalski/skills/tree/main/skills/emil-design-eng): claridad, jerarquía, feedback y accesibilidad.
- [break-ui](https://github.com/emilkowalski/skills/tree/main/skills/break-ui): datos extremos plausibles y estados adversos.
- [find-animation-opportunities](https://github.com/emilkowalski/skills/tree/main/skills/find-animation-opportunities): decidir dónde ayuda el movimiento y dónde perjudica la lectura.
- [animation-performance](https://github.com/emilkowalski/skills/tree/main/skills/animation-performance): propiedades animadas y trabajo durante cargas.
- [mobile-native](https://github.com/emilkowalski/skills/tree/main/skills/mobile-native): controles táctiles, entradas y restricciones de verificación en hardware.

No se agregaron librerías al producto. Se conservan MUI, MUI X Charts Community y TanStack Query.

## Superficie de datos revisada

| Valor visible | Origen | Tipo / límite revisado | Ausencia y extremos |
| --- | --- | --- | --- |
| Nombre de usuario | `/users.items[].username` | Texto, 64 caracteres en BD | Lista pendiente o error; nombre largo, diacríticos |
| Nombres de conexiones | Prop `connections[].name` | Texto, 128 caracteres en BD | Lista vacía, nombre largo, conexiones MOCK excluidas |
| Proveedores de filtros/gráficos/rutas | Tipo de conexión y `providers` | MEGA, Drive, Seafile, SMB y Nextcloud | Todos, una nube, cinco nubes; sin copias |
| Rango y zona horaria | Filtros y respuesta del API | Días inclusivos, máximo 366; zona IANA | Fechas incompletas, invertidas y entre años |
| Totales y estados de transferencia | `summary.transfers` | Contadores y estados agregados | Cero, uno, millones; sin denominador de éxito |
| Archivos y bytes | `summary.files` | Contadores y bytes conocidos; tamaños faltantes se cuentan aparte | Cero, millones, PiB; renombrados solapados con copiados |
| Días de actividad | `activity` y rango aplicado | Máximo 366 días | Días omitidos por el API, rango vacío, dos años |
| Serie por nube | `providers.source/destination` | Agregados por proveedor y rol | Un mismo archivo en las dos series; bytes frente a archivos |
| Etapas y duración | `performance.stages` | Etapa, proveedor/rol, muestras, ms y bytes/s | Mediana/P95/velocidad nulas; fallidas y sin cierre; 1.284 h |
| Intervalos de estado | `performance.states` | Estado, muestras, mediana y P95 | Lista vacía; nombres largos; duración en días |
| Tamaños | `reliability.size_distribution` | Seis categorías conocidas | Cero elementos, una categoría, tamaño desconocido |
| Códigos de error | `reliability.errors` | Código seguro, 64 caracteres en BD; hasta 10 frecuentes | Código largo sin espacios y conteos grandes |
| Extensiones | `reliability.extensions` | Seis extensiones más frecuentes devueltas por API | Lista vacía; literal escapado por React |
| Rutas | `providers.pairs` | Combinación origen/destino, archivos, bytes y trabajos | Más de seis rutas; acceso a las restantes |
| Espera y actividad | `scheduling` y `live` | Lectura actual, motivos y velocidad reportada | Cero, múltiples motivos, servicio sin respuesta |
| Cobertura | `coverage` | Trabajos con/sin manifiesto y archivos con/sin fecha | Cero registros; cobertura parcial; accesible también a USER |
| Salud y consumidores | `system.services/queues` | Estados, consumidor y pendientes; pendientes nullable | Cada estado, cola sin consumidor, lectura obsoleta |
| Conexiones administrativas | `system.connections` | Agregados del último estado guardado | Sin prueba; no confundir con prueba cloud en vivo |
| Notificaciones/evaluaciones | `system.notifications/evaluations` | Totales por estado | Sin tareas, fallos y estados diversos |
| Hora y acciones | `generated_at`, estado de Query | Timestamp, carga, error, pausa de actualización | Carga inicial, API 503, pausa, actualización y reintento |

Los contadores no tienen un máximo de presentación específico en el contrato. Se ensayaron 123.456.789 archivos y 1 PiB; no se recortan cifras para hacerlas caber. La precisión de enteros superiores al límite seguro de JavaScript sigue siendo una limitación del contrato numérico, no se inventa precisión en la UI.

## Hallazgos principales con datos adversos

| # | Severidad | Campo / situación | Valor o condición | Qué ocurría | Corrección |
| --- | --- | --- | --- | --- | --- |
| 1 | Broken | Filtros | Primera petición pendiente o error 503 | Desaparecía todo el formulario; no había forma de corregir filtros o reintentar | Shell persistente en `frontend/src/MetricsDashboard.tsx` |
| 2 | Broken | Salud y datos | Refresco 503 tras una lectura correcta | Se retiraban las últimas cifras, sin distinguir un fallo de actualización | Conservar lectura, fecha y aviso; salud sin verificar |
| 3 | Broken | Ejes X | Actividad y proveedores en navegador real | Existían nodos SVG de texto sin contenido por el alto disponible de las etiquetas | Altura de eje explícita de 44 px; comprobación en DOM y capturas |
| 4 | Broken | Advertencias | 5 omitidos + 7 fallidos + 30 renombrados | Se mostraba 42 como si fueran resultados independientes | Mostrar las 2 transferencias completadas con advertencias del fixture |
| 5 | Broken | Salud global | Cola de evaluaciones sin consumidor, resto disponible | La tarjeta seguía verde según el estado agregado recibido | La presentación considera también todas las colas devueltas |
| 6 | Fragile | Días ausentes | Copias el 16 y 18, ninguna el 17 | El gráfico conectaba ambos puntos sin representar el día vacío | Serie completa del rango con cero en días sin actividad |
| 7 | Fragile | Datos largos | Código de error de hasta 64 caracteres, conexión de 128 | Chips truncados; texto secundario invadía el bloque vecino en el ancho intermedio | Chips con salto de línea, límites de flex/grid y tablas con scroll local |
| 8 | Fragile | Medidas | Velocidad de 0,5 B/s o duración de 1.284 h | Índice de unidad negativo o minutos enormes | Unidades acotadas, PiB y duraciones en horas/días |

`Broken` significa que el dato o la acción no se podía interpretar/usar correctamente; `Fragile` identifica límites que dependen de valores reales. Hallazgos 1–8 tratados en esta revisión.

## Evaluación y mejoras por bloque

| Bloque | Antes (Before) | Después (After) | Motivo / verificación |
| --- | --- | --- | --- |
| Encabezado | Banner de gran peso visual; sin fecha general ni modo de actualización | Encabezado compacto, hora de lectura y controles de refresco | Priorizar cifras y comunicar vigencia |
| Datos de prueba | Switch ADMIN y opciones MOCK en filtros | Opción eliminada; filtros cloud/conexión excluyen MOCK siempre | Petición explícita; se mantiene el default de datos reales del API |
| Período | Cada selección iniciaba una consulta | Borrador + Aplicar filtros | Aplicar varias condiciones en una petición |
| Rango personalizado | Dejar una fecha vacía reemplazaba toda la vista | Fechas con etiquetas; validación junto a los campos; conservar resultados aplicados | Se puede corregir sin perder contexto |
| Máximo y orden de fechas | Validación solo del servidor | Validación local de ambas fechas, orden y 366 días | Evitar peticiones inválidas |
| Nube y conexión | Cambio parcial enviaba consultas sucesivas | Proveedor cambia y limpia la conexión incompatible en el borrador | No aplicar combinaciones imposibles de forma accidental |
| Usuario | Fallo de lista sin explicación | Error propio y reintento; no derriba el tablero | Cargas independientes |
| Restablecimiento | Sin acción de limpieza conjunta | Restablecer aplica defaults conocidos | Recuperación rápida tras filtrar |
| Carga inicial | Spinner que retiraba el contenido completo | Filtros visibles y esqueletos estáticos de tarjetas y gráficos | Mantener estructura y evitar saltos |
| Refresco | Una línea entraba y salía del flujo | Espacio fijo para progreso, botón ocupado y lectura previa visible | Evitar salto de layout y clics repetidos |
| Error inicial | Alerta sin acción | Alerta con Reintentar y filtros utilizables | Recuperación directa |
| Error de refresco / offline | Se ocultaban cifras o quedaba ambigua su vigencia | Última lectura conservada; aviso; salud/consumidores sin verificar | No afirmar salud actual con una consulta fallida |
| Peticiones obsoletas | Sin signal de cancelación | AbortSignal y llave de consulta con usuario/rol | Evitar aceptar respuestas para filtros/sesiones anteriores |
| Actualización dinámica | Polling sin controles visibles | Cada 30 s con pestaña visible, hora y pausa voluntaria | El usuario conoce y puede detener cambios mientras lee |
| Pausa del refresco | No existía | Lectura pausada y servicios sin verificar; no confundir con pausar transferencias | Las cifras quedan quietas sin prometer salud actual |
| Transferencias | Total sin indicar fechas aplicadas | Resumen separado de Estado actual con rango y zona visibles | Explicar qué período se consulta |
| Archivos / cobertura | Faltaban avisos de trabajos sin detalle a USER | Cobertura de manifiestos visible para todos y aviso de copias sin fecha | Mostrar límites del dato |
| Tasa de éxito | 0% sin trabajos terminados | Guion y explicación cuando no hay denominador | Diferenciar ausencia de evidencia de un fracaso |
| Advertencias | Suma de categorías de archivos que se solapan | Transferencias con advertencias; archivos omitidos/renombrados como contexto | Mantener una unidad inequívoca |
| Actividad diaria | Fechas recortadas a mes/día; huecos omitidos | Calendario completo, fechas localizadas, líneas rectas y tabla accesible | Comparación fiel y lectura fuera del gráfico |
| Proveedores | Título volumen con datos de archivos; etiquetas insuficientes | Alternar Archivos/Volumen, barras horizontales y tabla de cifras | Unidad correcta y nombres legibles |
| Series origen/destino | Podía interpretarse como doble volumen sumable | Explicación explícita de que no se suman ambas series | El mismo archivo aparece en sus dos roles |
| Rendimiento | Ratio de muestras sin explicar; fallos/incompletas ocultos | Correctas/total, fallidas y sin cierre; explicación de mediana y P95 | Juzgar cobertura y fallos de cada etapa |
| Tiempo por estado | Tabla siempre abierta | Desplegable accesible con el detalle completo | Reducir densidad inicial sin retirar datos |
| Resultado de archivos | Torta por tamaño dentro de un título genérico | Resultados separados y distribución titulada con leyenda de conteos | Distinguir resultado de distribución |
| Códigos / extensiones | Texto que se podía cortar en un chip | Saltos de línea, estados de error y seis extensiones frecuentes | Mantener identificadores completos |
| Rutas | Seis filas sin forma de consultar el resto; criterio poco claro | Orden por copias y desplegable con las restantes | Ranking y exhaustividad claros |
| Cola y tareas | Datos actuales junto al historial | Sección Estado actual con hora y explicación de alcance | La fecha histórica no limita esa lectura |
| Salud | READY/ready/DEGRADED crudos y fallos poco visibles | Estados en español, colores y texto, consumidor ausente visible | Comprensión sin depender solo del color |
| Conexiones | Podía parecer una prueba de nube en vivo | Desplegable Último estado guardado con explicación | No se introducen sondeos cloud costosos |
| Tareas administrativas | Estados crudos y vacío sin explicación | Estados traducidos, fallos destacados y vacío explícito | Interpretación rápida |
| Tablas y accesibilidad | Solo representación visual del gráfico; encabezados sin scope | Alternativa tabular, caption, scope y región con foco/scroll | Acceso por teclado y a las cifras exactas |
| Pantallas estrechas | Dos columnas demasiado pronto; scroll oculto por tarjetas; filtros largos antes de las cifras | Una columna hasta 1.100 px, cifras tabulares, overflow local y Más filtros desplegable en ancho estrecho | Comprobación a 320, 768, 1.440 y 2.560 px |
| Entradas táctiles | Fuentes pequeñas y blancos de pulsación variables | Entradas de 16 px y controles de al menos 44 px con puntero grueso | Evitar autozoom en iOS y facilitar pulsación |

No se modifican las definiciones del backend: los totales de trabajos/manifiestos agrupan por creación de la transferencia; la actividad de copias y las etapas usan su fecha efectiva. Por eso una serie diaria puede contener actividad de trabajos anteriores al rango de creación. La vista lo indica.

## Movimiento: decisiones y coste

| Oportunidad | Antes (Before) | Después (After) | Propósito, frecuencia y coste |
| --- | --- | --- | --- |
| Pulsación de botones | Feedback no específico del tablero | `scale(.98)`, 120 ms, `cubic-bezier(.23,1,.32,1)` | Feedback breve; solo transform; sin efecto en foco de teclado |
| Consulta activa | Línea que añadía altura y pulsaba indefinidamente | Altura reservada, transición de opacity de 140 ms | Estado de consulta sin mover contenido |
| Accesibilidad | Movimiento sin adaptación específica | Transiciones/movimiento retirados con reduced motion; spinner estático | Respetar preferencia sin perder el texto de estado |

Candidatos descartados:

- Animar líneas/barras/torta en cada sondeo: el usuario compara datos funcionales; `skipAnimation` en los tres tipos de gráfico.
- Contadores que suben hasta la cifra: retrasan la lectura y ocultan el valor efectivo.
- Entrada escalonada de tarjetas: es una pantalla de consulta frecuente; no aporta explicación.
- Animación de apertura de tablas/desplegables o filtros por teclado: perjudica rapidez y exige animar alto/layout.

No se agregaron `will-change`, bucles por frame, librerías de movimiento ni nuevos sondeos a proveedores. El cambio de propiedades animadas se verificó en código; no se certifican FPS de un teléfono a partir de un navegador de escritorio.

## Verificación y límites

La revisión visual usa fixtures sintéticos del mismo contrato del API, a través de fetch, en un navegador Chromium separado del despliegue. El selector Normal/Extremos/Vacío/Carga/Error vive exclusivamente en el harness temporal de revisión; no forma parte del bundle del producto.

- Datos normales, extremos, vacíos, carga y error a 320, 768, 1.440 y 2.560 px.
- Conexiones largas, conteos grandes, códigos largos, duración de varios días y velocidad inferior a 1 B/s.
- Refresco automático, cambio de consumidor, pausa voluntaria y fallo de consulta con conservación de resultados, usando reloj controlado; no se detuvieron servicios reales.
- Pruebas de componentes cubren filtros durante carga/error, aplicación conjunta, AbortSignal, fechas reparables, advertencias sin solapamiento, cola sin consumidor, actualización de cifras y alcance USER.

Pendiente de verificar en hardware: teclado y autozoom de Safari/iOS, interacción táctil real y GPU de un teléfono. La emulación de anchura no sustituye esa validación.

## Recomendaciones de siguiente alcance

1. **Salud desde fuera del backend.** Un monitor externo debe comprobar frontend, proxy y backend para detectar su caída aunque el dashboard no pueda cargar.
2. **Endpoint operativo independiente.** Separar salud/colas de los agregados históricos permitiría refrescar su estado con menos coste si se necesita un intervalo inferior a 30 segundos. Hoy el API conserva una caché de hasta 15 segundos.
3. **Navegación al detalle.** Abrir historial con filtros desde una cifra requiere un contrato de filtros compartido con el historial; no hacer clic en gráficos sin un destino útil.
4. **Comparación con el período anterior.** Debe agregarse en servidor con el mismo alcance y cobertura; no inventar tendencias calculadas con datos parciales del cliente.
5. **Observabilidad del host.** CPU/RAM, cuotas y alarmas requieren fuentes y política de permisos/retención; no dibujar indicadores sin medición real.

## Resultado del cierre

- Suite final del frontend: **75 pruebas aprobadas** en 7 archivos, incluidas 9 de Métricas. Lint y build de producción aprobados.
- Matriz de navegador: **20 escenarios** (5 estados × 4 anchos), sin excepciones JavaScript ni desbordamiento general de página. El scroll horizontal de tablas es local e intencional.
- Texto ampliado al **200%** a 320 y 1.440 px: sin cifras recortadas ni desbordamiento de página. Verificados selección de conexión larga, fechas inválidas/reparadas, máximo de rango, restablecimiento y movimiento reducido.
- Refresco con reloj controlado: consulta automática, cambio de salud ante consumidor ausente, pausa que detiene el sondeo y error que conserva cifras. Se usaron respuestas sintéticas, no se interrumpieron servicios reales.
- Se reconstruyó y publicó el frontend en `https://transfer.cloudlab.cl/metrics`; página HTTP 200 y bundle nuevo comprobado. Se verificó que el bundle no contiene el control eliminado ni el harness/fixtures locales. El frontend quedó saludable; no hubo cambios adicionales de esquema.
- Pendiente de revisión humana: recorrido autenticado con los datos de la instalación y validación táctil en teléfono físico.
