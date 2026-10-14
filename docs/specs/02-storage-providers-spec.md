# Especificación 02: Abstracción de Proveedores de Almacenamiento y Cifrado

## 1. Contexto y Objetivos
Proporcionar una arquitectura desacoplada para interactuar con diversos servicios de almacenamiento en la nube. El sistema interactúa con los proveedores mediante una interfaz común (`StorageProvider`), mientras que los secretos y tokens de acceso se almacenan cifrados en reposo en PostgreSQL.

---

## 2. Interfaz Base de Proveedor (`StorageProvider`)

Todo backend de almacenamiento debe implementar la siguiente interfaz tipada en Python:

```python
from abc import ABC, abstractmethod
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

class StorageItem(BaseModel):
    name: str
    path: str
    is_dir: bool
    size_bytes: Optional[int] = None
    modified_at: Optional[datetime] = None
    mime_type: Optional[str] = None
    provider_type: str

class PaginatedItems(BaseModel):
    items: List[StorageItem]
    next_cursor: Optional[str] = None
    total_count: Optional[int] = None

class ConnectionTestResult(BaseModel):
    success: bool
    message: str
    error_code: Optional[str] = None
    quota_total_bytes: Optional[int] = None
    quota_used_bytes: Optional[int] = None

class StorageQuota(BaseModel):
    total_bytes: Optional[int]
    used_bytes: Optional[int]
    free_bytes: Optional[int]

class StorageProvider(ABC):
    @abstractmethod
    async def test_connection(self) -> ConnectionTestResult:
        """Prueba conectividad y permisos listando el directorio raíz."""
        pass

    @abstractmethod
    async def list_items(
        self,
        path: str,
        cursor: Optional[str] = None,
        page_size: int = 50,
        sort_by: str = "name",
        direction: str = "asc",
        search: Optional[str] = None
    ) -> PaginatedItems:
        """Lista carpetas y archivos en la ruta especificada."""
        pass

    @abstractmethod
    async def get_item(self, path: str) -> Optional[StorageItem]:
        """Obtiene metadatos de un elemento específico."""
        pass

    @abstractmethod
    async def get_quota(self) -> Optional[StorageQuota]:
        """Obtiene la cuota de almacenamiento disponible si el proveedor la soporta."""
        pass
```

---

## 3. Implementaciones Iniciales del MVP

### A. Adaptador MEGA (`MegaCmdProvider`)
- **Parámetros requeridos:** Correo de la cuenta (`username`) y contraseña (`password`), cifrados en la base de datos.
- **Motor:** El binario oficial MEGAcmd gestiona la sesión, exploración, cuota, descargas y subidas de MEGA; Rclone no se usa como backend MEGA.
- **Seguridad y ciclo de vida:** El login se envía a `mega-cmd` por stdin (nunca como argumento del proceso). Cada sesión usa un `HOME` privado y efímero, ejecuta logout/quit y elimina su caché al cerrarse.
- **Límites de espera:** Login: 30 s; listado/cuota: 10 s; logout/quit: 3 s por cliente, con cierre acotado a 5 s por paso. Las tuberías heredadas por el daemon no pueden dejar el cliente esperando indefinidamente. Un logout agotado no impide intentar quit; si quit falla, se conserva el HOME y se informa el fallo. Las descargas/subidas no tienen este límite de metadatos.
- **Comandos permitidos:** `mega-ls`, `mega-df`, `mega-get`, `mega-put -c`, `mega-mkdir -p` (solo carpetas de destino), `mega-logout` y `mega-quit`; no se invocan comandos remotos destructivos.
- MEGA no garantiza hashes ni fechas de modificación en todas las respuestas; los metadatos opcionales permanecen como `None` cuando no están disponibles.
- El parser de `mega-ls -l` excluye los encabezados de columnas (`FLAGS ... NAME`) y los títulos de directorio con ruta absoluta antes de crear elementos, paginar o contar archivos. Un archivo real llamado `NAME` se conserva.
- Las transferencias con MEGA se realizan por archivo individual mediante staging local temporal, que se elimina antes de procesar el siguiente archivo.

### B. Adaptador Google Drive (`RcloneGoogleDriveProvider`)
- **Motor:** Rclone CLI aislado, exclusivamente para Google Drive.
- Los tamaños negativos de Rclone (`-1` en documentos nativos exportados) se presentan como `None`. La ruta Drive → Nextcloud exporta esos archivos a staging privado y verifica la exportación materializada mediante SHA-256 del destino, sin comparar tamaños desconocidos ni generar una segunda exportación para verificar.
- **Flujo de Autorización:** OAuth 2.0 (Client ID, Client Secret, Refresh Token y Access Token).
- **Soporte de Unidades:**
  - Mi Unidad (`root`).
  - Unidades Compartidas (*Shared Drives*): Parámetro de configuración `team_drive`.
  - Renovación automática de tokens mediante el Refresh Token cuando expire el Access Token.

### C. Adaptadores Seafile (`SeafileRcloneProvider` y `SeafileApiProvider`)
- `provider_type` es `SEAFILE`. `adapter_type` se guarda por conexión y acepta `RCLONE` (predeterminado) o `SEAFILE_API`; no se cambia automáticamente al fallar una operación.
- `RCLONE` usa el backend nativo de Rclone `type = seafile`. `SEAFILE_API` solo se usa cuando un administrador lo selecciona al crear o editar la conexión.
- Se autentica en el servidor mediante `/api2/auth-token/`; el token de cuenta queda dentro del payload cifrado. El OTP se acepta únicamente en la solicitud de prueba de conexión, no se persiste ni se devuelve.
- La API directa usa endpoints de bibliotecas, directorios, metadatos, enlaces de descarga y carga fragmentada. Los enlaces devueltos por Seafile deben permanecer en el mismo host configurado.
- El fileserver de Seafile normaliza nombres a NFC. En cargas con `Content-Range` interpreta `filename*` mediante MIME y luego `QueryUnescape`; para cargas multipart sin rango toma el nombre de la parte `file`. El adaptador debe conservar ambos formatos y devolver la ruta que el servidor confirme en la respuesta `ret-json`.
- `replace=false` evita sobrescribir un nombre que aparezca durante la carga, pero Seafile puede guardar el nuevo archivo con un nombre único. Si la ruta confirmada difiere de la solicitada (salvo equivalencia NFC), la transferencia falla con `SEAFILE_DESTINATION_NAME_CONFLICT` y expone ambas rutas en el detalle técnico.
- El adaptador API está implementado para operaciones de navegación y copia; los flujos que lo usan descargan o suben un archivo a la vez mediante staging privado. No añade operaciones de eliminación, movimiento o sincronización.
- Los fallos HTTP y de streaming se sanitizan; redirecciones HTTP no se aceptan como contenido de archivo. Cuando el listado informa el tamaño de un archivo API de origen, el worker exige que la descarga tenga ese tamaño antes de copiarla.
- Toda subida a Seafile API se vuelve a leer y se compara con SHA-256 del origen antes de contar el archivo como completado. En transferencias grandes esto añade una lectura de red del contenido subido.
- La compatibilidad de lanzamiento se limita inicialmente a Seafile Community Edition 13.x. Otras versiones y Professional no se consideran comprobadas por defecto; se debe ejecutar el test optativo contra la instancia de destino antes de habilitarlas.

### D. Proveedor Mock (`MockStorageProvider`)
- Implementación en memoria que simula un árbol de directorios con archivos ficticios y latencias programables.
- **Uso obligatorio en la Fase 2** para construir y validar el *Vertical Slice* de la aplicación antes de depender de credenciales reales.

### E. Recursos SMB/NAS (`RcloneSmbProvider`)
- SMB se registra como `provider_type=SMB` y permite explorar un recurso compartido y una carpeta base configurada. El host debe estar en `SMB_ALLOWED_TARGETS`; una lista vacía deniega el acceso.
- En el flujo de migración de la UI, el NAS es origen y MEGA, Google Drive o Seafile es destino. La aplicación no crea ni administra la VPN o las rutas de red necesarias.
- Rclone usa SMB únicamente desde el backend/worker, con configuración temporal por trabajo y la misma allowlist de comandos. El worker inventaría el árbol en un manifiesto persistido por páginas y registra el resultado por archivo.
- La credencial queda cifrada como las demás conexiones. El host, recurso compartido y carpeta base forman la raíz lógica visible como `/`. La UI excluye SMB del selector de destino, pero `POST /api/v1/transfers` no valida ese límite por tipo de proveedor; agregar o mantener una restricción de servidor si SMB debe ser solo origen.
- La guía operativa y sus límites están en [`docs/integrations/smb-nas-setup.md`](../integrations/smb-nas-setup.md).

### F. Nextcloud (`NextcloudRcloneProvider`)
- Nextcloud usa Rclone WebDAV (`type = webdav`, `vendor = nextcloud`) y una contraseña de aplicación. Un administrador introduce la URL HTTPS base, el usuario y la contraseña desde la UI; el backend genera `https://{host}/{prefijo-opcional}/remote.php/dav/files/{usuario}/`. Se aceptan servidores configurados por conexión, sin allowlist de despliegue.
- La UI muestra Nextcloud como proveedor sin flags de habilitación. La conexión queda inactiva hasta pasar la prueba; solo los administradores pueden registrar el servidor y sus credenciales.
- Las credenciales se almacenan cifradas con el mecanismo común y nunca se devuelven al frontend. Rclone se ejecuta como subproceso con argumentos separados y configuración temporal `0600`; la lista blanca de comandos y la semántica exclusivamente `COPY` no cambian.
- El listado usa la abstracción común y una caché local, acotada por tiempo y memoria. Las actualizaciones de credenciales usan otra clave de caché.
- Para trabajo Nextcloud se limita Rclone a un transfer, dos checkers, `NEXTCLOUD_BANDWIDTH_LIMIT` (predeterminado `8M`), `NEXTCLOUD_CHUNK_SIZE` (predeterminado `10Mi`), conexión de 10 s, inactividad de 60 s y `NEXTCLOUD_JOB_TIMEOUT_SECONDS` (predeterminado 12 h). La admisión permite por defecto un trabajo Nextcloud activo por clúster.
- Soporte de lanzamiento y versión real deben registrarse tras probar contra la instancia concreta; la existencia del adaptador no equivale a certificación operativa. Véase [`docs/integrations/nextcloud-compatibility.md`](../integrations/nextcloud-compatibility.md).

---

## 4. Cifrado en Reposo de Credenciales

```
+-------------------------------------------------------+
| Docker Secret: MASTER_ENCRYPTION_KEY (32 bytes / 256) |
+-------------------------------------------------------+
                           |
                           v
+-------------------------------------------------------+
|  AES-256-GCM Encryption Engine                         |
|  - Nonce aleatorio (12 bytes) por cada registro       |
|  - AAD (Additional Authenticated Data) = connection_id|
|  - Tag de autenticación GCM (16 bytes)                |
+-------------------------------------------------------+
                           |
                           v
+-------------------------------------------------------+
|  PostgreSQL: storage_connections.encrypted_credentials|
+-------------------------------------------------------+
```

### Reglas Críticas:
1. La clave maestra se inyecta desde `/run/secrets/master_key` montado en el contenedor; **NUNCA** se guarda en la base de datos ni en imágenes.
2. Cada registro de credencial se cifra con un Nonce único de 96 bits.
3. Se vincula el UUID de la conexión como AAD (`Associated Authenticated Data`) para evitar que un bloque cifrado sea transferido a otra conexión.
4. El frontend **NUNCA** debe recibir ni visualizar las credenciales descifradas. Al consultar una conexión existente, la API responde únicamente con `credentials_configured: true`.

---

## 5. Esquema de Base de Datos para Conexiones

```sql
CREATE TABLE storage_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(128) NOT NULL,
    provider_type VARCHAR(32) NOT NULL CHECK (provider_type IN ('MOCK', 'MEGA', 'GOOGLE_DRIVE', 'SEAFILE', 'SMB', 'NEXTCLOUD')),
    adapter_type VARCHAR(32) NOT NULL DEFAULT 'RCLONE' CHECK (adapter_type IN ('RCLONE', 'SEAFILE_API')),
    status VARCHAR(16) NOT NULL DEFAULT 'INACTIVE' CHECK (status IN ('ACTIVE', 'INACTIVE', 'ERROR')),
    deleted_at TIMESTAMP WITH TIME ZONE NULL,
    encrypted_credentials BYTEA NOT NULL,
    credential_version INTEGER NOT NULL DEFAULT 1,
    root_path VARCHAR(512) NULL,
    is_global BOOLEAN NOT NULL DEFAULT TRUE,
    last_tested_at TIMESTAMP WITH TIME ZONE NULL,
    last_test_status VARCHAR(64) NULL,
    last_error_code VARCHAR(64) NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    created_by UUID NOT NULL REFERENCES users(id)
);

CREATE TABLE connection_permissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id UUID NOT NULL REFERENCES storage_connections(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    can_browse BOOLEAN NOT NULL DEFAULT TRUE,
    can_read BOOLEAN NOT NULL DEFAULT TRUE,
    can_write BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE(connection_id, user_id)
);
```

`adapter_type` solo selecciona el motor Seafile (`RCLONE` o `SEAFILE_API`); las otras conexiones conservan `RCLONE` como valor neutral por compatibilidad de esquema.

### Archivado lógico de conexiones

`DELETE /api/v1/connections/{connection_id}` archiva una conexión sin borrar su fila: las transferencias históricas conservan sus referencias y eventos. Solo `ADMIN` puede realizar la operación y el endpoint requiere CSRF. El archivado borra `encrypted_credentials` y revoca todos los permisos privados; no llama al proveedor ni modifica archivos remotos. No existe restauración: para volver a usar el proveedor se debe crear y autenticar una conexión nueva.

La operación devuelve `204 No Content` al completarse, `404 Not Found` para UUID inexistentes o ya archivados y `409 Conflict` cuando haya una transferencia asociada en `QUEUED`, `VALIDATING`, `RUNNING`, `RETRYING` o `CANCELLING`. La creación de transferencias y el archivado bloquean las filas de conexión en orden determinista para que una conexión archivada no reciba trabajos nuevos. Las conexiones archivadas se excluyen de listados y de navegación, prueba, OAuth, edición, asignación de permisos y nuevas transferencias.

---

## 6. Estrategia de Pruebas (TDD)
- **Unitarias:**
  - `test_encryption_roundtrip`: Cifrado y descifrado de credenciales devuelve el payload exacto.
  - `test_encryption_tampered_data`: Modificar un solo bit del ciphertext lanza `InvalidTagException`.
  - `test_aad_mismatch`: Descifrar credenciales con un `connection_id` diferente falla.
  - `test_mock_storage_provider_navigation`: El proveedor mock permite navegar niveles jerárquicos.
- **Integración:**
  - `test_test_connection_endpoint`: Endpoint `/api/v1/connections/{id}/test` ejecuta y actualiza `last_tested_at`.
  - `test_archive_connection_preserves_transfer_history`: Archivar una conexión inactiva borra ciphertext y permisos, sin eliminar transferencias o eventos.
  - `test_archive_connection_rejects_active_transfers`: Una transferencia en cualquiera de los estados activos bloquea el archivado con `409`.
