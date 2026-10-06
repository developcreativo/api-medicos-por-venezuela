---
name: nueva-funcionalidad
description: Construye una funcionalidad nueva en la API (FastAPI + Supabase) siguiendo el flujo del repo — spec → plan → todo en tasks/<cambio>/, rama feat/* desde dev, tests ≥95 %, ruff, Swagger, migración con artisan. Usar cuando el usuario pida "agregar", "implementar", "construir" o "nueva funcionalidad" en api-medicos-por-venezuela.
---

# Nueva funcionalidad (API)

Flujo obligatorio para cualquier capacidad nueva de `api-medicos-por-venezuela`. No se escribe
código de producto antes del paso 3.

## 0. Contexto que se carga siempre

Lee, en este orden, antes de proponer nada:

1. `CLAUDE.md` y las cuatro reglas de `.claude/rules/` (`security.md`, `commands.md`,
   `db_env.md`, `fastapi_skills.md`). Si la funcionalidad toca mensajería, correo o WhatsApp,
   también `.claude/rules/mensajeria.md` y la skill `mensajeria`.
2. `.knowledge/` — la lógica de negocio real (`business-logic.md`, `agenda.md`,
   `interconsultas.md`, `mensajeria.md`). Lo que dice aquí manda sobre lo que "parece" el código.
3. `tasks/` — si ya existe una carpeta para este cambio, se continúa; no se abre otra.
4. `git log --oneline -20` y `git branch -r --sort=-committerdate | head` — otro desarrollador
   (Leonardo Alvarado) trabaja en paralelo; revisa si hay una rama `feat/*` o `fix/*` abierta
   sobre lo mismo antes de empezar.

## 1. Spec — `tasks/<cambio>/spec.md`

Formato del repo (copia la estructura de `tasks/interconsulta-asincrona/spec.md`):

- Encabezado con fecha y origen de la intención (chat de Workana, videollamada, issue).
- `## Objective`: qué gana cada actor (paciente, médico, admin). Tabla de actores.
- `## Historias de usuario` numeradas; cada una verificable.
- `## Tech Stack` y `## Commands` (copiar del spec anterior; no inventar comandos).
- `## Project Structure`: archivos que se tocan o crean, por repo.
- `## Requisitos` R1..Rn con criterios de aceptación concretos (qué devuelve, qué código HTTP,
  quién puede). Cada requisito nombra el permiso RBAC que lo protege.
- `## Fuera de alcance` explícito.
- `## Preguntas abiertas`: lo que el cliente no ha confirmado. **No se decide por él**: se
  deja la pregunta y la tarea que depende de ella queda bloqueada hasta tener respuesta.

Si la funcionalidad abarca los dos repos, la spec canónica vive aquí (dominio) y el frontend
tiene una copia corta en su `tasks/<cambio>/spec.md` que enlaza a esta.

## 2. Plan — `tasks/<cambio>/plan.md`

- Decisiones de arquitectura numeradas con su porqué (formato de `tasks/*/plan.md`).
- Orden de fases: **migración → modelo → servicio (+ tests) → router (+ tests) → correo/eventos
  → frontend**. Cada fase deja `main` desplegable; un `git revert` intermedio no rompe nada.
- Estimación en horas por fase (el proyecto se cobra por horas en Workana y se reporta).

## 3. Todo — `tasks/<cambio>/todo.md`

Casillas `- [ ] T1 …` agrupadas por fase, con un "Checkpoint" al final de cada fase:
`0 failed, cobertura ≥95 %, ruff limpio, migrate limpio`. Se marcan al terminar cada una.

## 4. Construcción — reglas no negociables

- **Rama:** `feat/<cambio>` desde `dev`. PR contra `dev`; `main` se alimenta por PR desde `dev`.
  Nunca commit directo en `dev` ni en `main`.
- **Migración:** `python artisan make:migration <descripcion>`; transaccional e idempotente
  (`IF NOT EXISTS`, `ON CONFLICT`). Permisos nuevos se **siembran en la migración**
  (`permissions` + `role_permissions`), nunca a mano. Tablas nuevas con datos de personas: RLS
  deny-all (la API entra como dueña; el navegador no lee nada por PostgREST).
- **Capas:** router delgado → servicio (lógica, queries, excepciones de dominio de
  `src/core/errors.py`) → modelo. Cero queries en routers. Proteger endpoint =
  `Depends(require_permission("recurso.accion"))`.
- **Pertenencia (IDOR):** el servicio verifica que el llamante tiene derecho sobre el objeto, en
  **todas** las rutas que lo mutan, incluidos sub-recursos.
- **Estados disputados:** `UPDATE … WHERE <estado esperado>` y `rowcount`; jamás read-then-write.
- **Contenido clínico o de conversación:** columna `EncryptedText("tabla.columna")`; salida vía
  esquema con `ClinicalAccessMixin`/grant; nunca en logs, correos, Excel ni `WHERE`.
- **Listados:** paginados (`limit`/`offset`, máx. 100) con `order_by` terminado en columna única.
- **Correo:** siempre por `services/mail.py` con `best_effort` y `BackgroundTasks`; un correo caído
  nunca rompe el flujo. Respetar `notifications.NOTIFICATION_EVENTS` y `should_send`.
- **Swagger:** `summary`, docstring, `responses` (404/409/422), `tags` descritos en `main.py`.
- **Tests:** por cada servicio y endpoint; concurrencia con `asyncio.gather` donde haya estado
  disputado; savepoints. Sin tests no hay PR.

## 5. Cierre

```bash
uv run pytest --cov=src --cov-report=term-missing --asyncio-mode=auto   # ≥95 %
uv run ruff check . --fix && uv run ruff format .
python artisan migrate:status                                            # sin pendientes
```

- Marca las casillas del `todo.md` y anota horas reales por fase al pie (para el reporte en
  Workana).
- Si la funcionalidad cambia lógica de negocio, actualiza `.knowledge/<modulo>.md` y el README
  (sección de endpoints). El README y `.knowledge/` describen lo que **hay**, no lo que se planea.
- Commit: Conventional Commits en español, un commit por unidad revisable (`feat(mensajeria): …`,
  `test(mensajeria): …`). PR con descripción de qué y por qué, y el enlace a `tasks/<cambio>/`.
