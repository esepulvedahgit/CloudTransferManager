# ADR-001: Ejecución Aislada de Rclone por Subproceso vs API Remota RC

## Estado
Aceptado

## Contexto
Rclone ofrece dos mecanismos para ser controlado programáticamente:
1. **Rclone Remote Control (RC) API:** Un servidor HTTP que expone métodos RPC para gestionar transferencias y configuración.
2. **Subproceso CLI Aislado:** Invocar el binario de Rclone (`rclone copy ...`) directamente desde workers en segundo plano.

La documentación oficial de Rclone advierte que exponer la API RC equivale a otorgar acceso de terminal (shell) al usuario que ejecuta el proceso, permitiendo ejecución arbitraria de comandos, lectura del sistema de archivos y volcado de credenciales en memoria si no está perfectamente protegida.

## Decisión
En Cloud Transfer Manager se opta estrictamente por la **ejecución aislada mediante subprocesos CLI controlados** invocados por los workers de Celery, descartando la exposición de la API RC.

### Reglas aplicadas:
- Cada trabajo ejecuta un subproceso independiente con una lista de argumentos inmutables (`list[str]`), prohibiendo terminantemente `shell=True`.
- Se aplica una lista blanca cerrada de comandos permitidos (`copy`, `lsjson`, `about`, `size`).
- La configuración de credenciales se escribe en un archivo efímero con permisos `0600` que se destruye inmediatamente tras el término o cancelación del trabajo.

## Consecuencias
- **Positivas:** Máximo aislamiento de privilegios; eliminación total del vector de inyección de comandos remotos vía HTTP; consumo nulo de memoria persistente por demonios RC inactivos.
- **Negativas:** La obtención de métricas de progreso requiere parsear la salida JSON generada por los flags `--use-json-log --stats 1s` de Rclone a través de tuberías (`pipes`), lo cual se resuelve de forma eficiente con un parser asíncrono en Python.
