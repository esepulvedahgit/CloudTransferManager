# Especificación 05: Telemetría en Tiempo Real y Server-Sent Events (SSE)

## 1. Contexto y Objetivos
Proporcionar una experiencia fluida y transparente en la interfaz web mostrando el avance segundo a segundo de las transferencias activas sin recargar la página, utilizando Server-Sent Events (SSE) sobre HTTP y un bus de eventos en Redis.

---

## 2. Arquitectura de Telemetría

```
[ Worker / MEGAcmd o Rclone Subprocess ]
             |
             | (Parser JSON de Rclone y progreso numérico de MEGAcmd)
             v
      [ Redis Pub/Sub ]  <--- Canal: `transfer:{job_id}:progress`
             |
             v
   [ FastAPI SSE Endpoint ]
             |
             | (EventStream `text/event-stream`)
             v
  [ Frontend React Hook ]
```

---

## 3. Formato del Payload de Progreso (SSE)

```json
{
  "job_id": "8e84620d-7a44-4ea4-a526-dab19de6b81c",
  "public_id": "TRF-2026-000001",
  "status": "RUNNING",
  "progress_percent": 64.5,
  "files_completed": 12,
  "files_total": 20,
  "bytes_transferred": 134217728,
  "bytes_total": 209715200,
  "current_speed_bps": 5242880,
  "average_speed_bps": 4819200,
  "estimated_remaining_seconds": 15,
  "current_item": "/Proyectos/video_demo.mp4",
  "error_count": 0,
  "attempt_count": 1,
  "manifest_summary": {"copied": 12, "failed": 0, "skipped": 0, "pending": 8, "renamed": 1},
  "evaluation": {
    "status": "EVALUATING",
    "generation": 1,
    "sequence": 3,
    "files_found": 3200,
    "count_complete": false,
    "max_files": 5000,
    "started_at": "2026-10-10T10:14:00Z",
    "updated_at": "2026-10-10T10:15:32Z",
    "completed_at": null,
    "safe_error_code": null,
    "safe_message": null
  },
  "timestamp": "2026-10-10T10:15:32Z"
}
```

El primer frame es un snapshot tomado después de suscribirse al bus e incluye `manifest_summary`, incluso cuando la transferencia ya terminó. Los frames de progreso posteriores incluyen el mismo resumen actualizado.

### Reglas de Presentación en la UI:
- **Total Desconocido:** Si el proveedor no puede reportar el total de bytes o archivos de antemano, mostrar una barra indeterminada animada, indicando los bytes y archivos transferidos hasta el momento.
- En rutas con staging, `bytes_transferred` mide el avance lógico del archivo una sola vez y no debe superar el tamaño conocido de origen. Presentar unidades binarias con etiquetas `KiB`, `MiB`, `GiB` y `TiB`.
- Mostrar `VALIDATING` como «Validando» y `RETRYING` como «Reintentando», indicando el próximo número de intento. Al fallar, consultar `/events` y mostrar el mensaje de `FAILURE_DIAGNOSTIC`; los detalles técnicos no incluyen rutas completas ni texto crudo del proveedor.
- **Varias Rutas de Origen:** Si el inventario previo informa el total de bytes, calcular el avance contra ese total fijo, incluidas carpetas. Rclone descubre su total durante la copia y puede informar 100% para un subconjunto inicial; ese porcentaje no representa el trabajo completo. Para trabajos sin bytes conocidos, usar una estimación por ruta. Si el trabajo total de una transferencia de varias rutas es desconocido, omitir el ETA en vez de presentar el ETA de una sola ruta como si fuera el del trabajo completo.
- El parser de MEGAcmd acepta únicamente métricas numéricas reconocidas; salida desconocida se ignora. `current_item` conserva la ruta remota y nunca expone una ruta local de staging ni la línea cruda del proceso.
- **Resiliencia ante Desconexión:** El hook del frontend debe implementar reconexión automática con backoff si se pierde temporalmente la conexión SSE.
- **Persistencia en Segundo Plano:** Cerrar o refrescar la pestaña del navegador **NUNCA** cancela el trabajo en ejecución. Al volver a abrir la página, el frontend se suscribe nuevamente al stream y recupera el estado.
- **Cierre del progreso terminal:** Al terminar una transferencia, mostrar su estado final durante 3 segundos, refrescar el historial y la carpeta destino y después retirar la tarjeta de progreso activo. El registro permanece en el historial.
- **Transferencias programadas:** Los trabajos con fecha programada, dependencia, modo exclusivo o motivo de espera permanecen en **Programaciones** y no ocupan las tarjetas grandes de progreso ni el historial mientras sigan activos. Un indicador compacto del encabezado muestra el total y abre un panel con el motivo de espera o los bytes/progreso actual. Las acciones para reprogramar y cancelar permanecen en **Programaciones**, cuya lista pagina trabajos activos para que los más antiguos no desaparezcan detrás de páginas recientes.
- **Evaluación anticipada:** El snapshot, listado y eventos incluyen el DTO público de evaluación. El conteo de archivos se presenta aparte del avance de copia. El frontend combina `generation` y `sequence` para ignorar eventos atrasados; una cantidad menor se acepta cuando empieza una generación nueva. El polling de 15 segundos recupera el último estado persistido tras perder SSE o recargar la página.
- **Suscripción y recuperación:** El cliente abre SSE para transferencias activas, incluidas las programadas que ya comenzaron. Al llegar a estado terminal, quita el trabajo del indicador y actualiza el historial. Las listas de historial, programadas y segundo plano se recuperan cada 15 segundos mientras la pestaña está visible; el polling se pausa en pestañas de fondo. Así, una terminalización cuyo evento SSE se perdió aparece en el historial en el siguiente ciclo de recuperación.
- **Conteo SMB en historial:** Para transferencias SMB, presentar los contadores agregados del manifiesto (`copied`, `failed`, `skipped`) junto a los bytes. `files_completed` es un conteo de elementos resueltos y no debe rotularse como número de copias exitosas.

---

## 4. Endpoint de Streaming

```http
GET /api/v1/transfers/{transfer_id}/stream
Accept: text/event-stream
```

---

## 5. Estrategia de Pruebas (TDD)
- **Unitarias:**
  - `test_rclone_json_stats_parser`: Convierte correctamente las líneas de log JSON de Rclone en el modelo de métricas de telemetría.
- **Integración:**
  - `test_sse_stream_receives_redis_pubsub_message`: Publicar un evento en Redis resulta en un frame SSE emitido al cliente HTTP.
