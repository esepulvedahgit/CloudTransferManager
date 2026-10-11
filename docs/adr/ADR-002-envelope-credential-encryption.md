# ADR-002: Cifrado en Reposo de Credenciales mediante AES-256-GCM y Master Key Externa

## Estado
Aceptado

## Contexto
Cloud Transfer Manager almacena contraseñas y tokens OAuth para conectar con proveedores de almacenamiento en la nube. Si la base de datos PostgreSQL sufriera una exfiltración o volcado no autorizado, dichas credenciales no deben quedar accesibles en texto claro.

## Decisión
Implementar cifrado autenticado en reposo utilizando **AES-256-GCM** combinado con inyección externa de clave maestra:

1. **Master Key Externa:** La clave de cifrado de 256 bits se suministra en tiempo de ejecución a través de Docker Secrets (`/run/secrets/master_key`) y el entrypoint la copia a un `tmpfs` privado (`/run/ctm-secrets/master_key`) antes de bajar privilegios. Nunca se escribe en código fuente, imágenes Docker ni tablas de la base de datos.
2. **Nonce Único:** Cada registro cifrado genera un IV/Nonce aleatorio criptográfico de 96 bits.
3. **Associated Authenticated Data (AAD):** El identificador UUID de la conexión (`storage_connection_id`) se enlaza como AAD durante el cifrado y descifrado. Esto garantiza que un texto cifrado no pueda ser trasplantado a otra conexión en la base de datos sin disparar un error de autenticación criptográfica.
4. **Almacenamiento de Contraseñas de Usuario:** Se separa tajantemente el cifrado de credenciales externas del hashing de usuarios. Las contraseñas de los usuarios del sistema se procesan exclusivamente con **Argon2id**.

## Consecuencias
- **Positivas:** Protección de nivel empresarial contra filtración de copias de seguridad de la base de datos; integridad y autenticidad criptográfica verificadas.
- **Negativas:** La pérdida de la Master Key externa imposibilita la recuperación de las credenciales de proveedores configuradas (requeriría volver a introducirlas).
