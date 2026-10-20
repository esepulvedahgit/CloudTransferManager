# Diseño: CI de calidad y seguridad para `main`

## Objetivo

Mantener `develop` como rama local de trabajo y ejecutar verificaciones automáticas para cada pull request dirigido a `main` y cada cambio integrado en `main`. El flujo debe revisar calidad, pruebas, vulnerabilidades de dependencias e imágenes, secretos y configuración antes de considerar el código listo para producción.

## Contexto y restricciones

- Al iniciar este trabajo, `develop` ya existe, está activa y apunta al mismo commit que `main`; no hay workflows bajo `.github/workflows/`.
- El proyecto usa Python 3.12, FastAPI, Ruff, mypy y pytest en backend; React, TypeScript, Vitest y Vite en frontend.
- Backend y worker se construyen desde `backend/Dockerfile`; frontend se construye desde `frontend/Dockerfile`.
- `compose.yaml` necesita `.env`, Docker Secrets, PostgreSQL y Redis. El CI no debe iniciar Compose ni conectarse a proveedores externos.
- Los checks no requieren credenciales reales. Ningún secreto de proveedores ni llave de cifrado debe añadirse al workflow.
- El workflow se limitará a `contents: read`; no publicará SARIF ni requerirá GitHub Advanced Security.
- El repositorio mantiene una allowlist estricta para llamadas a Rclone. Este CI no debe añadir operaciones Rclone ni cambiar su configuración.

## Enfoque seleccionado

Crear un workflow de GitHub Actions con eventos `pull_request` cuyo destino sea `main`, `push` a `main` y ejecución manual (`workflow_dispatch`). Los jobs no desplegarán la aplicación.

### Calidad y pruebas

- Backend: instalar dependencias runtime y de desarrollo; ejecutar `ruff check .`, `pytest -v --tb=short` y `mypy app` desde `backend/`.
- Frontend: ejecutar `npm ci`, `npm test`, `npm run lint` y `npm run build` desde `frontend/`.
- Separar backend y frontend en jobs independientes para aislar fallos y habilitar ejecución paralela.

### Vulnerabilidades con Trivy

- Ejecutar un escaneo `fs` sobre el repositorio para detectar vulnerabilidades declaradas en manifiestos y secretos, y un escaneo `config` para revisar Dockerfiles y Compose.
- Construir la imagen backend/worker y la imagen frontend con sus Dockerfiles actuales, sin levantar los servicios ni pasar credenciales; escanear ambas imágenes por vulnerabilidades del sistema operativo y paquetes.
- Hacer fallar el check ante hallazgos `HIGH` o `CRITICAL` con corrección disponible. Mostrar hallazgos sin corrección para revisión, sin ocultarlos del reporte.
- Mantener reportes en el log/resumen del job; no habilitar permisos de escritura de seguridad para subir SARIF.
- Mantener el análisis sin excepciones globales iniciales. Si aparecen hallazgos existentes, corregirlos o documentar excepciones precisas con responsable y vencimiento antes de normalizarlas.

### Ejecución y protección

- Declarar permisos mínimos de lectura y no exponer secretos del repositorio a los jobs de PR.
- Fijar las GitHub Actions externas a versiones estables verificables (preferiblemente SHA completo) y revisar sus permisos.
- Para impedir merges cuando fallen los checks, configurar en GitHub las reglas de protección de `main` y marcar los checks como requeridos. Esta política remota no se puede activar desde el archivo workflow local.
- No publicar ni modificar ramas remotas durante este cambio. La rama activa de trabajo seguirá siendo `develop`.

## Herramienta complementaria recomendada

Semgrep Community Edition para análisis estático de código propio (Python/FastAPI y TypeScript/React). Complementa a Trivy: busca patrones vulnerables y errores de seguridad en el código que no aparecen necesariamente como CVE de dependencias o de imagen. Se recomienda evaluarla como siguiente etapa; no se integra en este workflow inicial.

## Alternativa descartada

Escanear únicamente archivos del repositorio dejaría sin revisar paquetes del sistema operativo instalados por los Dockerfiles. Levantar todo Compose añadiría credenciales, estado y servicios que no son necesarios para estas verificaciones. Por eso se compilan y escanean las dos imágenes de aplicación directamente, junto con el filesystem.

## Criterios de aceptación

1. `develop` permanece como la rama local activa; no se crea una rama duplicada.
2. Pull requests hacia `main` ejecutan todos los checks de calidad, pruebas y Trivy.
3. Los pushes a `main` vuelven a ejecutar los checks.
4. Ruff, pytest, mypy, pruebas frontend, chequeo TypeScript y build frontend tienen resultados independientes y visibles.
5. Trivy analiza filesystem, configuración, imagen backend/worker e imagen frontend, y aplica el umbral de severidad aprobado.
6. Los jobs no requieren `.env`, Docker Secrets, credenciales de proveedores ni permisos de escritura del token de GitHub.
7. El documento indica cómo hacer obligatorios los checks mediante protección de `main`.
8. El archivo workflow pasa una validación YAML y una revisión de seguridad de permisos, eventos y referencias de Actions.

## Riesgos conocidos

- El primer escaneo puede detectar vulnerabilidades ya presentes y bloquear el check; es un resultado que requiere triage, no una falla de configuración por sí sola.
- Los escaneos de imágenes agregan tiempo al CI y dependen de descargar paquetes base y la base de vulnerabilidades de Trivy.
- GitHub no exige checks exitosos para el merge hasta que un administrador configure las reglas de protección de `main`.
