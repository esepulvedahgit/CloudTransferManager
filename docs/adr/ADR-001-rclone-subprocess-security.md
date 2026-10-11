# ADR-001: Ejecución Aislada de MEGAcmd/Rclone por Subproceso

## Estado
Aceptado

## Contexto
Rclone ofrece dos mecanismos para ser controlado programáticamente:
1. **Rclone Remote Control (RC) API:** Un servidor HTTP que expone métodos RPC para gestionar transferencias y configuración.
2. **Subproceso CLI Aislado:** Invocar el binario de Rclone (`rclone copy ...`) directamente desde workers en segundo plano.

La documentación oficial de Rclone advierte que exponer la API RC equivale a otorgar acceso de terminal (shell) al usuario que ejecuta el proceso, permitiendo ejecución arbitraria de comandos, lectura del sistema de archivos y volcado de credenciales en memoria si no está perfectamente protegida.

El backend MEGA de Rclone desplegado no está disponible en esta aplicación; MEGA se opera con el cliente oficial MEGAcmd, sin sustituir el motor Rclone que permanece para Google Drive.

## Decisión
En Cloud Transfer Manager se opta estrictamente por la **ejecución aislada mediante subprocesos CLI controlados** invocados por los workers de Celery, descartando la exposición de la API RC de Rclone. MEGAcmd es el adaptador para MEGA y Rclone permanece como motor de Google Drive.

### Reglas aplicadas:
- Cada invocación usa argumentos separados, prohibiendo terminantemente `shell=True`. El login de MEGAcmd se entrega por stdin y nunca expone la contraseña en argv.
- Rclone conserva su lista blanca cerrada (`copy`, `lsjson`, `about`, `size`); MEGAcmd usa solo sus clientes de listado, cuota, descarga, subida no destructiva y cierre.
- La configuración Rclone se escribe en un archivo efímero con permisos `0600`. Cada sesión MEGAcmd tiene un `HOME` privado, eliminado junto a su caché al finalizar.
- Las transferencias cruzadas usan staging disk-backed privado de un archivo a la vez; el archivo temporal se elimina en éxito, fallo o cancelación. Si ya existe la ruta de destino MEGA, se omite con advertencia.

## Consecuencias
- **Positivas:** Máximo aislamiento de privilegios; eliminación total del vector de inyección de comandos remotos vía HTTP; consumo nulo de memoria persistente por demonios RC inactivos.
- **Negativas:** El progreso de Rclone requiere parsear sus estadísticas JSON; MEGAcmd emite una barra de progreso textual que solo se convierte en métricas cuando contiene valores numéricos reconocidos.
