# Especificación 03: Explorador de Archivos y Navegación

## 1. Contexto y Objetivos
Proporcionar una experiencia visual intuitiva y ágil para explorar sistemas de archivos remotos, permitiendo seleccionar archivos individuales o carpetas completas en un origen y definir una ruta de destino para iniciar transferencias.

---

## 2. Diseño de Interfaz de Usuario

### A. Vista de Escritorio (Desktop >= 1280px)
- **Diseño de Doble Panel (*Dual-Pane*):**
  - **Panel Izquierdo (Origen):** Selector de conexión de origen, barra de ruta navegable (*breadcrumbs*), botón de subir nivel (`..`), buscador en tiempo real, tabla de elementos con checkboxes de selección múltiple.
  - **Panel Derecho (Destino):** Selector de conexión de destino, barra de ruta de destino, botón de crear carpeta, tabla de carpetas donde navegar para fijar el destino de copia.
- **Botón de Acción Central:** Botón *"Iniciar Transferencia"* habilitado únicamente cuando hay al menos 1 elemento seleccionado en el origen y una carpeta válida seleccionada en el destino.

### B. Vista Adaptativa Móvil (< 768px)
En dispositivos móviles, el doble panel se transforma automáticamente en un **Wizard por Pasos**:
1. **Paso 1:** Seleccionar conexión de origen y navegar para seleccionar elementos.
2. **Paso 2:** Seleccionar conexión de destino y seleccionar la carpeta de destino.
3. **Paso 3:** Seleccionar política de conflicto (`SKIP` u `OVERWRITE_IF_DIFFERENT`) y confirmar.

---

## 3. Modelo y Presentación de Metadatos

Cada elemento listado debe contener:
- `name`: Nombre del archivo o directorio.
- `path`: Ruta completa relativa a la raíz del proveedor.
- `is_dir`: Booleano (indica si es carpeta o archivo).
- `size_bytes`: Tamaño en bytes (o `null` para directorios o si el proveedor no lo expone).
- `modified_at`: Fecha ISO-8601 (o `null` en proveedores como MEGA que no siempre la entregan).
- `mime_type`: Tipo detectado.

### Reglas de Presentación:
- Formateo amigable de tamaño (ej. `14.2 MB`, `1.05 GB`).
- Iconografía diferenciada por tipo de archivo (carpetas, imágenes, videos, comprimidos, documentos, etc.).
- Skeleton loaders mientras se consultan las APIs de los proveedores remotos.
- Búsqueda en memoria sobre la página actual o llamada con filtro `?search=termino` al backend.

---

## 4. Endpoints de la API

```http
GET /api/v1/connections/{connection_id}/items?path=/&cursor=&page_size=50&sort=name&direction=asc&search=
GET /api/v1/connections/{connection_id}/items/metadata?path=/Proyectos/archivo.zip
```

---

## 5. Estrategia de Pruebas (TDD)
- **Unitarias:**
  - `test_path_sanitization_prevents_traversal`: Rutas maliciosas como `../../etc/passwd` son neutralizadas o rechazadas con `400 Bad Request`.
  - `test_sorting_and_filtering`: Filtrado correcto por término de búsqueda y ordenamiento por nombre/tamaño.
- **Integración:**
  - `test_explorer_endpoint_returns_paginated_items`: Endpoint devuelve lista de elementos y cursor de paginación.
