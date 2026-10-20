# Respaldos y recuperación con MEGA S4

El servicio opcional `backup` genera un `pg_dump` consistente cada 10 minutos, incluye `pg_dumpall --globals-only`, `.env` y los secretos de aplicación, cifra todo el paquete con AES-256-GCM y lo sube a un prefijo dedicado de MEGA S4. Solo se habilita con el perfil Compose `backup`; no modifica el servicio PostgreSQL existente ni publica puertos. El volumen `backup_state` conserva solo el estado del último ciclo y el bloqueo. Dumps, secretos temporales y paquetes del proceso de respaldo usan `/run/ctm-backup-work`, un `tmpfs` privado que desaparece al detener o recrear el contenedor.

Una restauración explícita descifra el contenido bajo la ruta que indiques. En los comandos de ensayo de esta guía, `/restore` se monta desde `./restore` en el host, así que allí quedan la base de datos y los secretos en claro hasta que los borres. Usa un host confiable con disco cifrado, limita el acceso al directorio y elimínalo después de validar la recuperación.

MEGA S4 implementa acceso S3 con Signature V4 y permite los estilos de bucket virtual-hosted y path-style. Hay que configurar el endpoint y región que MEGA muestra para el bucket; el endpoint puede cambiar según región. S4 no admite las cabeceras S3 de cifrado en servidor, así que CTM cifra el paquete localmente antes de cargarlo. Consulta la [especificación S3 de MEGA S4](https://github.com/meganz/s4-specs/blob/main/README.md) y la [lista vigente de endpoints](https://help.mega.io/megas4/setup-guides/mega-s4-endpoint-urls).

## Configuración

1. Crea un bucket S4 y una clave API con permisos mínimos para leer y escribir objetos dentro de ese bucket. Copia el access key y secret key en los archivos locales `secrets/mega_s4_access_key` y `secrets/mega_s4_secret_key`. No los agregues a `.env`, comandos, logs o Git.
2. Ejecuta `bash scripts/setup-mega-s4-backup-secrets.sh`. El script crea una clave de cifrado de respaldo de 32 bytes solo si no existe; guarda una copia fuera de este host, porque sin ella no se pueden descifrar los respaldos. No la confundas con `secrets/master_key`, que también se incluye cifrada dentro de cada paquete.
3. Configura en `.env` `BACKUP_ENABLED=true`, `BACKUP_S3_ENDPOINT=https://<endpoint-S3-de-tu-bucket>`, `BACKUP_S3_REGION=<region>`, `BACKUP_S3_BUCKET=<bucket>` y `BACKUP_S3_PREFIX=cloud-transfer-manager`. `BACKUP_INTERVAL_SECONDS=600` y `BACKUP_MAX_AGE_SECONDS=900` expresan la frecuencia de respaldo y el umbral de salud para el objetivo RPO de 15 minutos.
4. Habilita el servicio: `docker compose --profile backup up -d --build backup`. El daemon ejecuta un respaldo al iniciar y repite el ciclo cada 10 minutos. El resto del stack no requiere reinicio.

Revisa `docker compose ps backup` y `docker compose logs --tail=100 backup`. La marca `healthy` indica que el último respaldo completó la carga y verificación de tamaño/metadatos y no superó los 15 minutos. Una falla de S4 o una copia que tarde demasiado deja el servicio `unhealthy`; configura una alerta del host sobre ese estado. No se afirma un RPO efectivo hasta medir duración y retraso con el tamaño real de la base.

## Ensayo aislado de restauración

El ensayo descarga un objeto exacto, valida AES-GCM y SHA-256, comprueba que `pg_restore` reconoce el archivo y verifica que la `master_key` restaurada puede descifrar todas las credenciales activas. El destino vive bajo `restore/`, excluido de Git y protegido con permisos `0700/0600`.

1. Obtén el `object_key` más reciente de los logs del servicio backup. El destino contendrá datos y secretos descifrados en el filesystem del host; ejecuta el ensayo solo en un host confiable y bórralo al terminar. Ejecuta:

   ```bash
   install -d -m 0700 restore
   docker compose --profile backup run --rm --no-deps --user 0:0 \
     -e CTM_BACKUP_RUN_AS_ROOT=true backup \
     python3 -m app.backup restore \
     --object-key 'cloud-transfer-manager/YYYY/MM/DD/ctm-backup-YYYYMMDDTHHMMSSZ-<uuid>.ctm' \
     --output '/restore/ensayo-<fecha>'
   ```

2. Arranca una instancia PostgreSQL temporal aislada. `RESTORE_DIR` debe ser la ruta absoluta al directorio creado en el paso anterior:

   ```bash
   RESTORE_DIR="$PWD/restore/ensayo-<fecha>" \
     docker compose -p ctm-restore-test -f compose.restore.yaml up -d restore-postgres
   docker compose -p ctm-restore-test -f compose.restore.yaml exec -T -u 0 restore-postgres \
     pg_restore --no-owner --no-privileges --username cloud_transfer \
     --dbname cloud_transfer --host /var/run/postgresql --exit-on-error \
     /restore/database.dump
   RESTORE_DIR="$PWD/restore/ensayo-<fecha>" \
     docker compose -p ctm-restore-test -f compose.restore.yaml run --rm restore-verifier
   ```

   `restore-verifier` informa solo el número de conexiones descifradas y nunca imprime credenciales. Si el despliegue agrega roles o tablespaces además de `cloud_transfer`, revisa `globals.sql` y restáuralos de forma apropiada antes de habilitar la aplicación.

3. Registra hora de inicio/fin, tamaño del respaldo y la edad del punto recuperado. Al terminar, elimina únicamente el proyecto de prueba y su volumen temporal:

   ```bash
   docker compose -p ctm-restore-test -f compose.restore.yaml down -v
   rm -rf -- restore/ensayo-<fecha>
   ```

No uses el proyecto de prueba conectado a la red o los volúmenes del despliegue activo. Este servicio no respalda Redis. En una recuperación real, conserva workers y Beat detenidos; tras restaurar la base y antes de habilitarlos, ejecuta `psql -v ON_ERROR_STOP=1 -f scripts/reconcile-restored-transfers.sql` contra la base restaurada. Eso marca como fallidos los trabajos no terminales, revoca sus tokens de despacho, falla notificaciones pendientes y registra un evento para cada trabajo que requiere revisión. Verifica cada destino con el proveedor antes de crear una copia nueva para evitar duplicados. El ensayo descrito no certifica todavía el RTO de dos horas ni el RPO de 15 minutos.

## Límites

- El respaldo lógico completo se agenda cada 10 minutos; el tiempo de generación y carga depende del tamaño de PostgreSQL, la CPU y la conexión de salida. Si una ejecución supera el umbral de edad, Compose marca el servicio no saludable.
- Configura `BACKUP_TMPFS_SIZE` y `BACKUP_MEMORY_LIMIT` según la base de datos. Durante el empaquetado coexisten el dump, el tar y el artefacto cifrado, por lo que reserva aproximadamente tres veces el tamaño del dump más margen. El valor inicial es `2G` de tmpfs y `3G` de memoria; si el tmpfs se llena, el ciclo falla sin subir un paquete parcial. Linux puede mover páginas de tmpfs a swap: cifra el swap del host o desactívalo si los datos temporales no deben llegar a almacenamiento persistente sin cifrar.
- Redis, certificados TLS y las credenciales API S4 no se incluyen en el paquete. Conserva la clave de cifrado de respaldo fuera del host y mantiene el procedimiento para reconstruir la configuración de despliegue.
- No se borran objetos antiguos automáticamente. MEGA S4 no documenta soporte de lifecycle/versionado en su especificación S3; hasta definir y probar una política de retención, el prefijo crece.
- No se ejecutó una carga/restauración real contra una cuenta MEGA en esta implementación. Hace falta endpoint, bucket y claves locales para completar esa evidencia.
