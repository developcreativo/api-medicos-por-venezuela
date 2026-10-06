---
name: corregir-bug
description: Corrige un comportamiento existente de la API (FastAPI + Supabase) con el flujo reproducir → test que falla → fix mínimo → verificación → registro. Usar cuando el usuario reporte un error, un 500, un dato mal, "no funciona", "arreglar" o "corregir" en api-medicos-por-venezuela.
---

# Corregir un bug (API)

## 1. Reproducir antes de tocar

1. Lee `CLAUDE.md`, `.claude/rules/security.md` y el `.knowledge/<modulo>.md` del área afectada.
   Muchos "bugs" son reglas de negocio deliberadas (gate de credencial, cola por especialidad,
   fail-closed del cifrado clínico). Si el comportamiento está documentado como intencional, no es
   un bug: se reporta al usuario y se para.
2. Localiza la ruta: router → servicio → modelo → migración. Cita `archivo:línea`.
3. Reproduce con un test async en `tests/` que **falle** hoy (savepoints; datos sembrados en el
   propio test, nunca contra Supabase de producción). Si la reproducción exige concurrencia, usa
   `asyncio.gather` + `httpx.AsyncClient`.
4. Revisa `git log -S"<símbolo>" --oneline` y las ramas remotas recientes: puede que otro
   desarrollador ya lo esté corrigiendo o que un merge reciente lo haya introducido.

## 2. Diagnóstico

Escribe en dos líneas: causa raíz y por qué el test existente no lo atrapó. Si la causa está en
otra capa (RLS, trigger de Postgres, frontend), dilo antes de parchear el síntoma aquí.

## 3. Fix mínimo

- Solo la ruta del bug. Sin refactors de paso, sin "ya que estoy".
- Si el bug es de pertenencia o autorización, `grep` por **todas** las rutas que mutan ese recurso
  y sus sub-recursos y aplica el mismo guard en cada una (lección del review 2026-07-14).
- Si el bug es de concurrencia, la corrección es una escritura condicional
  (`UPDATE … WHERE … ; rowcount == 0 → 409`), no un `try/except` más.
- Si el bug está en una migración ya aplicada, se corrige con **una migración nueva**
  idempotente; nunca se edita una aplicada.
- Si el criterio de credencial cambia, cambia `doctors._blocked_reason` **y**
  `public.doctor_can_practice` en el mismo PR (`tests/test_rls_policies.py` lo vigila).

## 4. Verificación

```bash
uv run pytest --cov=src --cov-report=term-missing --asyncio-mode=auto
uv run ruff check . --fix && uv run ruff format .
```

El test de reproducción pasa a verde y ningún otro cambia. Cobertura ≥95 %.

## 5. Registro

- Rama `fix/<que-arregla>` desde `dev`, PR a `dev`. Commit `fix(<scope>): <qué y por qué>`;
  el cuerpo cita el test que lo fija.
- Si el bug revela una regla que faltaba, añade una línea a la regla correspondiente en
  `.claude/rules/` (formato "lección del review AAAA-MM-DD") o a `.knowledge/`.
- Si el bug lo reportó el cliente por Workana, anota horas y resumen en `tasks/backlog-correcciones.md`.
