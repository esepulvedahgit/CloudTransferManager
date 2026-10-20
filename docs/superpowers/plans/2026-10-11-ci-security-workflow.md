# Plan de implementación: CI de calidad y seguridad para `main`

> Plan aprobado requerido antes de implementar. La rama de trabajo es `develop`.

**Objetivo:** Ejecutar calidad, pruebas y escaneos de seguridad en pull requests hacia `main`, pushes a `main` y bajo solicitud manual.

**Diseño:** `docs/superpowers/specs/2026-10-11-ci-security-workflow-design.md`

**Rama local:** `develop` ya existe, está activa y coincide actualmente con `main`. No crear una segunda rama ni publicar ramas remotas durante el trabajo.

## Restricciones

- Mantener el workflow sin credenciales reales, `.env`, Docker Secrets, servicios de Compose o proveedores externos.
- No alterar el motor Rclone, su allowlist ni las operaciones de almacenamiento.
- Conceder al token de Actions solo `contents: read`; no subir SARIF ni pedir permisos extra.
- Fijar las Actions externas a SHA completo de una versión estable revisada.
- Mantener backend/worker como una sola imagen porque ambos usan `backend/Dockerfile`.
- El workflow no despliega ni publica imágenes.

## Task 1: Agregar workflow de calidad y seguridad

**Archivo:** `.github/workflows/ci-security.yml`

1. Definir eventos `pull_request` con destino `main`, `push` en `main` y `workflow_dispatch`.
2. Establecer `permissions: contents: read` en el nivel del workflow y no definir secrets para los jobs.
3. Crear job `backend` con Python 3.12:
   - instalar `backend/requirements.txt` y `backend/requirements-dev.txt`;
   - ejecutar desde `backend/`: `ruff check .`, `pytest -v --tb=short` y `mypy app`;
   - usar solamente configuración de prueba efímera si la suite necesita variables de entorno.
4. Crear job `frontend` con Node 22:
   - ejecutar `npm ci`, `npm test`, `npm run lint` y `npm run build` desde `frontend/`.
5. Crear job `security` independiente:
   - escanear el filesystem con Trivy para vulnerabilidades de manifiestos y secretos;
   - escanear Dockerfiles y `compose.yaml` en modo `config`;
   - construir localmente `ctm-backend:ci` desde `backend/` y `ctm-frontend:ci` desde `frontend/`;
   - escanear ambas imágenes por CVEs de sistema operativo y librerías; backend cubre también el worker por compartir Dockerfile;
   - producir una pasada de reporte con todos los hallazgos `HIGH` y `CRITICAL`, y una pasada de gate que falle por hallazgos de esas severidades con corrección disponible; los casos sin corrección quedan visibles en el reporte;
   - no iniciar Compose ni entregar credenciales durante el build.
6. Usar nombres estables de jobs/checks y pasos para que puedan marcarse como requeridos en la protección remota de `main`.

## Task 2: Verificar el cambio

1. Validar el YAML y la sintaxis/seguridad del workflow con `actionlint` si está disponible en el entorno.
2. Ejecutar `git diff --check` e inspeccionar el contenido del workflow, confirmando eventos, referencias SHA, permisos, rutas de build y umbrales Trivy.
3. Confirmar que la rama actual sigue siendo `develop` y que no se tocó `main` ni se publicaron commits.
4. No ejecutar localmente las suites de producto: el workflow recién creado las ejecutará en GitHub Actions; informar si la validación estática detecta que el entorno no dispone de `actionlint`.

## Configuración remota posterior

En GitHub, configurar las reglas de protección de `main` para exigir los checks de backend, frontend y seguridad. El archivo local no puede activar las reglas del repositorio remoto.

Semgrep CE queda como recomendación para una siguiente etapa, sin agregarlo al workflow inicial.

## Criterios de aceptación

- `develop` sigue activa al finalizar.
- `.github/workflows/ci-security.yml` corre en PR a `main`, push a `main` y despacho manual.
- Calidad, pruebas y seguridad aparecen como checks independientes.
- Trivy analiza filesystem, configuración, imagen de backend/worker e imagen frontend con la política acordada.
- No hay secretos reales, permisos de escritura, despliegues ni comandos Rclone en el workflow.
- La validación estática del archivo no reporta errores.
