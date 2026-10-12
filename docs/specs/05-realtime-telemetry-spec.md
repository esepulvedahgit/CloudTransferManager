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
  "timestamp": "2026-10-10T10:15:32Z"
}
```

### Reglas de Presentación en la UI:
- **Total Desconocido:** Si el proveedor no puede reportar el total de bytes o archivos de antemano, mostrar una barra indeterminada animada, indicando los bytes y archivos transferidos hasta el momento.
- **Varias Rutas de Origen:** Ponderar el avance por bytes cuando todas las rutas son archivos con tamaño conocido; si no, usar una estimación equitativa por ruta. Si el trabajo total de una transferencia de varias rutas es desconocido, omitir el ETA en vez de presentar el ETA de una sola ruta como si fuera el del trabajo completo.
- El parser de MEGAcmd acepta únicamente métricas numéricas reconocidas; salida desconocida se ignora. `current_item` conserva la ruta remota y nunca expone una ruta local de staging ni la línea cruda del proceso.
- **Resiliencia ante Desconexión:** El hook del frontend debe implementar reconexión automática con backoff si se pierde temporalmente la conexión SSE.
- **Persistencia en Segundo Plano:** Cerrar o refrescar la pestaña del navegador **NUNCA** cancela el trabajo en ejecución. Al volver a abrir la página, el frontend se suscribe nuevamente al stream y recupera el estado.
- **Cierre del progreso terminal:** Al terminar una transferencia, mostrar su estado final durante 3 segundos, refrescar el historial y la carpeta destino y después retirar la tarjeta de progreso activo. El registro permanece en el historial.

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
