# Prueba de compatibilidad Seafile

Fecha de revisión de las referencias: 2026-10-11.

## Alcance declarado

El soporte inicial de esta integración se limita a **Seafile Community Edition 13.x**. La existencia de endpoints compatibles o una prueba exitosa en Professional no demuestra compatibilidad con Community Edition. No habilites otra versión como soportada hasta ejecutar y registrar este harness contra esa versión y edición.

La aplicación selecciona el adaptador guardado en cada conexión: `RCLONE` (predeterminado) o `SEAFILE_API` (optativo). No cambia de adaptador automáticamente ante fallos. El test de conexión de la aplicación realiza autenticación/listado; para comprobar compatibilidad de lectura y escritura real, usa el test en vivo descrito aquí.

## Qué comprueba

`backend/tests/integration/seafile/test_live_seafile_ce13.py` queda omitido por defecto. Al activarlo:

1. Obtiene un token con las credenciales proporcionadas y consulta `/api2/server-info/`.
2. Falla si la instancia no informa versión `13.x` o incluye la característica `seafile-pro`.
3. Comprueba que la biblioteca indicada exista y que ambos adaptadores puedan autenticarse y listar.
4. Crea un directorio único `/ctm-compat-{UUID}` dentro de esa biblioteca de prueba.
5. Sube, lista, descarga y compara SHA-256 de archivos de unos 2 MiB mediante Rclone y Seafile API; la ruta API también prueba un archivo vacío, Unicode, espacios y carga en fragmentos de 1 MiB.
6. Elimina únicamente ese directorio UUID de prueba en un bloque `finally` mediante `DELETE /api2/repos/{repo_id}/dir/?p=...`.

La eliminación directa está confinada al test de integración. No se expone en la aplicación ni agrega un comando destructivo de Rclone. La limpieza requiere la confirmación literal indicada abajo; si la ejecución termina abruptamente antes del `finally`, revisa y elimina manualmente solo la carpeta `ctm-compat-{UUID}` creada por esa ejecución.

## Preparar el entorno aislado

- Usa una instancia descartable de **Community Edition 13.x**, no datos de producción.
- Crea una biblioteca vacía dedicada a la prueba, con permisos de lectura y escritura para el usuario técnico. Si es cifrada, configura también su contraseña.
- Instala la versión de Rclone que desplegarás y confirma que el backend `seafile` está incluido.
- Inyecta los valores siguientes desde un gestor de secretos o un archivo local fuera del repositorio. No los pongas en Git, argumentos de proceso, tickets ni logs:

| Variable | Requerida | Uso |
|---|---:|---|
| `SEAFILE_INTEGRATION=1` | Sí | Activa el test en vivo; sin esta variable el test se omite. |
| `SEAFILE_TEST_URL` | Sí | URL base de Seahub, sin endpoint API. |
| `SEAFILE_TEST_USERNAME` | Sí | Usuario técnico de la biblioteca descartable. |
| `SEAFILE_TEST_PASSWORD` | Sí | Contraseña de cuenta. |
| `SEAFILE_TEST_LIBRARY` | Sí | Nombre exacto de la biblioteca dedicada. |
| `SEAFILE_TEST_CONFIRM_CLEANUP=DELETE_CTM_COMPATIBILITY_PREFIX_ONLY` | Sí | Confirma la limpieza acotada al directorio único del test. |
| `SEAFILE_TEST_OTP` | No | Código OTP actual si la cuenta usa 2FA; no se almacena. |
| `SEAFILE_TEST_LIBRARY_PASSWORD` | No | Contraseña si la biblioteca está cifrada. |
| `RCLONE_BINARY` | No | Ruta del ejecutable; predeterminado `/usr/bin/rclone`. |

Con los secretos ya inyectados en el entorno del proceso:

```bash
cd backend
.venv/bin/pytest -v --tb=short tests/integration/seafile/test_live_seafile_ce13.py
```

Una ejecución sin `SEAFILE_INTEGRATION=1` debe terminar como `skipped`. Con el opt-in habilitado, si falta una variable requerida el test falla antes de crear datos. Un éxito comprueba la ruta de transferencia puntual de esta prueba; no constituye un benchmark ni demuestra carga de cientos de miles de archivos o varios TB.

## Cómo registrar evidencia

Guarda junto con el resultado, fuera de los logs que contengan secretos:

- versión exacta, edición y build de Seafile;
- versión exacta de Rclone y del backend distribuido;
- resultado de cada adaptador, operación, tamaño de archivo y fecha;
- errores sanitizados, si los hubo, y si la carpeta UUID fue limpiada.

No registres contraseña, OTP, token, contenido del archivo de configuración temporal ni URLs con credenciales.

## Referencias técnicas consultadas

- Código oficial de Seahub, ruta `/api2/server-info/` y `/api2/repos/`: [seahub/api2/urls.py](https://github.com/haiwen/seahub/blob/master/seahub/api2/urls.py), consultado el 2026-10-11.
- Implementación oficial de `ServerInfoView`: [seahub/api2/views_misc.py](https://github.com/haiwen/seahub/blob/master/seahub/api2/views_misc.py), consultado el 2026-10-11. Informa `version` y `features`; agrega `seafile-pro` cuando `is_pro_version()` es verdadero.
- API oficial de directorios: [Seafile Web API: Directories](https://cloud.seafile.com/published/web-api/v2.1/directories.md), consultado el 2026-10-11. Documenta listado, creación y eliminación de directorios y los parámetros `p` / `operation=mkdir`.
- Implementación local comprobable: [Seafile API provider](../../backend/app/providers/seafile_api.py), [Rclone Seafile provider](../../backend/app/providers/seafile.py), [Rclone runner](../../backend/app/services/rclone.py) y [test en vivo](../../backend/tests/integration/seafile/test_live_seafile_ce13.py).

Las referencias de código `master` son móviles; esta página registra cuándo se consultaron. El resultado reproducible del test contra la instancia configurada es la evidencia operativa para la versión exacta desplegada.
