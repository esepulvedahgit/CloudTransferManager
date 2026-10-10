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

### A. Adaptador MEGA (`RcloneMegaProvider`)
- **Parámetros requeridos:** Correo de la cuenta (`username`) y contraseña (`password`).
- **Particularidades técnicas:**
  - Rclone requiere que la cuenta de MEGA haya realizado un login inicial normal para derivar las llaves maestras de cifrado simétrico de MEGA.
  - MEGA no garantiza hashes MD5 ni fechas de modificación en todas las llamadas; los modelos deben tratar `modified_at` y `hash` como opcionales (`None`).
  - Comando de prueba interno: `rclone about <temp_remote>: --config <temp_file>` o `rclone lsf <temp_remote>: --max-depth 1`.

### B. Adaptador Google Drive (`RcloneGoogleDriveProvider`)
- **Flujo de Autorización:** OAuth 2.0 (Client ID, Client Secret, Refresh Token y Access Token).
- **Soporte de Unidades:**
  - Mi Unidad (`root`).
  - Unidades Compartidas (*Shared Drives*): Parámetro de configuración `team_drive`.
  - Renovación automática de tokens mediante el Refresh Token cuando expire el Access Token.

### C. Proveedor Mock (`MockStorageProvider`)
- Implementación en memoria que simula un árbol de directorios con archivos ficticios y latencias programables.
- **Uso obligatorio en la Fase 2** para construir y validar el *Vertical Slice* de la aplicación antes de depender de credenciales reales.

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
    provider_type VARCHAR(32) NOT NULL CHECK (provider_type IN ('MEGA', 'GOOGLE_DRIVE', 'S3', 'WASABI', 'MEGA_S4')),
    status VARCHAR(16) NOT NULL DEFAULT 'INACTIVE' CHECK (status IN ('ACTIVE', 'INACTIVE', 'ERROR')),
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

---

## 6. Estrategia de Pruebas (TDD)
- **Unitarias:**
  - `test_encryption_roundtrip`: Cifrado y descifrado de credenciales devuelve el payload exacto.
  - `test_encryption_tampered_data`: Modificar un solo bit del ciphertext lanza `InvalidTagException`.
  - `test_aad_mismatch`: Descifrar credenciales con un `connection_id` diferente falla.
  - `test_mock_storage_provider_navigation`: El proveedor mock permite navegar niveles jerárquicos.
- **Integración:**
  - `test_test_connection_endpoint`: Endpoint `/api/v1/connections/{id}/test` ejecuta y actualiza `last_tested_at`.
