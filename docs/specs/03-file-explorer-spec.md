# Especificación 03: Explorador de Archivos y Navegación

## 1. Contexto y Objetivos
Proporcionar una experiencia visual intuitiva y ágil para explorar sistemas de archivos remotos, permitiendo seleccionar archivos individuales o carpetas completas en un origen y definir una ruta de destino para iniciar transferencias.

---

## 2. Interfaz disponible actualmente

### A. Escritorio
- El explorador presenta origen y destino en paneles coordinados. Cada panel permite seleccionar conexión activa, navegar con breadcrumbs y subir un nivel; el origen permite búsqueda y selección múltiple.
- El destino se elige navegando una carpeta existente. La UI no ofrece creación manual de carpetas; durante la copia el motor crea la estructura necesaria para los elementos seleccionados.
- La acción de transferencia requiere al menos una ruta de origen seleccionada y conexiones/ruta de destino válidas.

### B. Pantallas estrechas
En pantallas estrechas, el flujo muestra el origen y el destino como pasos separados:
1. **Paso 1:** Seleccionar conexión de origen y navegar para seleccionar elementos.
2. **Paso 2:** Seleccionar conexión de destino y seleccionar la carpeta de destino.
3. La política de conflicto (`SKIP` o `OVERWRITE_IF_DIFFERENT`) y la notificación por correo se configuran en el resumen de la transferencia antes de confirmarla.

---

## 3. Modelo y Presentación de Metadatos

Cada elemento listado debe contener:
- `name`: Nombre del archivo o directorio.
- `path`: Ruta completa relativa a la raíz del proveedor.
- `is_dir`: Booleano (indica si es carpeta o archivo).
- `size_bytes`: Tamaño en bytes (o `null` para directorios o si el proveedor no lo expone).
- `modified_at`: Fecha ISO-8601 (o `null` en proveedores como MEGA que no siempre la entregan).
- `mime_type`: Tipo detectado.

### Reglas disponibles:
- Formateo amigable de tamaño (ej. `14.2 MB`, `1.05 GB`).
- Iconografía diferenciada por tipo de archivo (carpetas, imágenes, videos, comprimidos, documentos, etc.).
- Skeleton loaders mientras se consultan las APIs de los proveedores remotos.
- Búsqueda por carpeta a través de `?search=termino`; la paginación de resultados usa cursor y páginas de hasta 200 elementos (la UI solicita 50 por página).

---

## 4. Endpoints de la API

```http
GET /api/v1/connections/{connection_id}/items?path=/&cursor=&page_size=50&sort=name&direction=asc&search=
```

No existe actualmente un endpoint de metadatos individuales ni de creación manual de carpetas. El contrato de API mantenido está en [`docs/api/openapi-contracts.md`](../api/openapi-contracts.md).

---

## 5. Estrategia de Pruebas (TDD)
- **Unitarias y de API existentes:** validación de traversal, búsqueda/listado y cursor por proveedor; consultar `backend/tests/test_api_security.py`, `backend/tests/test_mock_provider.py`, `backend/tests/test_rclone_providers.py` y `backend/tests/test_megacmd_provider.py`.
- Los nombres de pruebas y su existencia documentan cobertura automatizada, no el resultado de una ejecución reciente. La matriz de estado indica las verificaciones pendientes.
