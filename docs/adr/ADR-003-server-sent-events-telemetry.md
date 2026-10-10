# ADR-003: Telemetría de Transferencias Unidireccional mediante Server-Sent Events (SSE)

## Estado
Aceptado

## Contexto
Durante la ejecución de transferencias de archivos, la interfaz web debe reflejar segundo a segundo métricas críticas como porcentaje, velocidad, bytes transferidos, archivo actual y tiempo estimado restante.

Opciones evaluadas:
1. **Short Polling REST:** Consultar cada 1-2 segundos un endpoint `/transfers/{id}`.
2. **WebSockets:** Conexión TCP dúplex continua entre navegador y servidor.
3. **Server-Sent Events (SSE):** Flujo unidireccional HTTP (`text/event-stream`) del servidor al cliente.

## Decisión
Se adopta **Server-Sent Events (SSE)** como el canal principal de telemetría:
- El flujo de datos durante una transferencia es estrictamente **unidireccional** (servidor hacia navegador). Las acciones del cliente (pausar, cancelar, reintentar) son esporádicas y se ejecutan naturalmente mediante peticiones HTTP REST estándar.
- SSE opera de forma transparente sobre HTTP estándar, simplificando el paso por proxies inversos (Nginx), balanceadores y cortafuegos sin requerir actualizaciones de protocolo complejas como WebSockets.
- Soporta reconexión automática nativa en el navegador y es más liviano en consumo de recursos.

## Consecuencias
- **Positivas:** Simplicidad arquitectónica en backend (FastAPI `EventSourceResponse`) y cliente (`EventSource` / React hook); compatibilidad nativa con Nginx.
- **Negativas:** Limitado a comunicación unidireccional (adecuado para telemetría).
