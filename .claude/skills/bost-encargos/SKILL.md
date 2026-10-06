---
name: bost-encargos
description: Cómo redactar un encargo para los subagentes bost (bost-backend, bost-frontend, bost-qa) con tres ejemplos reales. Cargar antes de delegar trabajo con la herramienta Agent desde el orquestador bost, o cuando un subagente devuelva trabajo fuera de alcance, incompleto o con afirmaciones que el código no respalda. Usar cuando el usuario diga /bost, "delega", "reparte", "encarga" o cuando una petición necesite varias zonas (datos, interfaz, pruebas).
---

# Cómo encargar trabajo a bost

Un subagente **no ve la conversación**. Lo único que sabe es lo que va en su prompt. Un encargo
flojo no produce trabajo flojo: produce trabajo que parece correcto y hay que rehacer.

La regla práctica: el encargo debe bastarle a alguien que acaba de llegar al proyecto, sin
preguntar nada. Si al leerlo quedan dudas sobre qué archivos tocar o qué no tocar, falta
contexto.

## Anatomía de un encargo

Siete piezas, en este orden:

1. **Rol inyectado.** Si los subagentes no están registrados (el harness lee `.claude/agents/`
   del **repo en el que arrancó la sesión**; si se abrió en un directorio padre no los
   encuentra), pega la definición del rol al principio del prompt y lanza con
   `subagent_type: general-purpose`. Comprueba antes dónde estás: `Agent type 'bost-qa' not
   found` casi siempre significa cwd equivocado, no agente ausente.
2. **Repo y stack**, con la ruta absoluta. Hay dos repos y se parecen.
3. **Qué leer primero.** `CLAUDE.md`, las reglas de `.claude/rules/` que apliquen, la spec, la
   skill del módulo. Nómbralas; no digas «lee las convenciones».
4. **Alcance decidido**, con lo que queda fuera por nombre propio. Ver más abajo.
5. **El trabajo**, numerado y ordenado por gravedad, con `archivo:línea` y el criterio de la
   spec que lo exige.
6. **Lo que NO debe hacer.** Es la pieza que más trabajo ahorra.
7. **Definition of Done**, con los comandos exactos y la orden de pegar su salida literal.

## El alcance se dice por nombre propio

«No amplíes el alcance» no funciona. Lo que funciona es enumerar lo excluido:

> **NO** implementes nada de `spec-chat-tiempo-real.md` (WebSocket, `call_sessions`,
> videollamadas desde el chat, `/me/threads`, `/typing`, `/presence/heartbeat`) ni nada de la
> Fase 2 de WhatsApp (R9–R11). Esas quedan explícitamente fuera.
>
> No toques `src/services/queue.py` (el claim atómico), `src/services/jitsi.py`, la cola por
> especialidad ni `registration_mail`.

Y cuando hay deuda conocida que **no** hay que arreglar, dilo, o el agente la «arreglará»:

> La columna `messages.call_session_id` apunta a una tabla `call_sessions` que no existe. Es
> deuda de la v2. **No la quites ni crees la tabla** — solo menciónala en tu informe.

## Ejemplo 1 — Verificación (a `bost-qa`)

Para «comprueba si lo que dice el documento es verdad». La clave es pedir **veredicto explícito
por afirmación** y prohibir inferir.

> `todo.md` afirma que estas tareas están **completadas** (`[x]`), y eso es exactamente lo que
> hay que comprobar contra el código:
>
> - T1.3 `src/services/messaging.py`: `thread_grant`, `send_message` (con adjuntos),
>   `list_messages`, `mark_read`, `inbox` (con presencia del paciente) + tests
> - Checkpoint: «0 failed, cobertura ≥95 %, ruff limpio, `migrate:status` sin pendientes»
>
> Para cada afirmación marcada `[x]`, da un veredicto explícito: **CONFIRMADO**, **PARCIAL** (y
> qué falta) o **FALSO** (no está en el código). Cita siempre `archivo:línea` o la salida exacta
> del comando como evidencia. Si no pudiste verificar algo, dilo en «No probado» con el motivo
> — **no lo infieras ni lo des por bueno**.
>
> Ejecuta las comprobaciones del proyecto y anota la salida **tal cual**:
> `uv run pytest --cov=src --cov-report=term-missing --asyncio-mode=auto`. Si no puede correr,
> anota el error exacto y marca la cobertura como NO VERIFICADA.

Qué añadir siempre a un encargo de verificación:

- **Prohibiciones de seguridad**, con el motivo. `python artisan migrate:status` escribe DDL
  (`scripts/migrate.py:125-128`) y el entorno puede apuntar al pooler de producción: no se
  ejecuta. Nada de inserts contra Supabase de producción. No arrancar Supabase.
- **La trampa del número agregado.** Pide la cobertura **por módulo**: un «97 %» del total del
  repo puede esconder módulos nuevos al 74 %.
- **Juzgar la calidad del test, no solo su existencia:** «mira unos cuantos y di si las
  aserciones son significativas o si solo ejecutan líneas».

## Ejemplo 2 — Corrección (a `bost-backend` / `bost-frontend`)

Un punto por hallazgo, ordenados por gravedad, cada uno con evidencia, el criterio que incumple
y el porqué. El porqué importa: sin él, el agente elige la solución cómoda.

> ### 1. Los adjuntos clínicos se guardan en `/tmp`, no en un bucket privado (CA15.3)
>
> `src/services/storage.py:12` → `_BASE_STORAGE_PATH = Path("/tmp/medico-storage") / …`.
>
> `spec.md` CA15.3 exige bucket **privado** con rutas
> `consultations/{consultation_id}/attachments/{attachment_id}.bin`. Son PDFs e imágenes
> clínicas: en `/tmp` quedan legibles por otros usuarios del host, se pierden al reiniciar el
> contenedor y son invisibles entre réplicas.
>
> Investiga el patrón que el proyecto ya use para hablar con Supabase antes de elegir el camino.
> Si no hay ninguna vía configurada, **no lo dejes en `/tmp` en silencio**: implementa la ruta
> con su configuración en `Settings`, haz que el arranque avise si falta, y explica en tu informe
> qué hace falta provisionar.

Dos cierres que conviene repetir en todo encargo de corrección:

> Si algún punto resulta imposible o choca con otra regla del repo, **detente en ese punto,
> termina los demás** y explícalo en el informe con el detalle del conflicto. No lo resuelvas
> inventando alcance nuevo.

> Sube la cobertura con tests que prueben comportamiento real —caminos de error, ramas de grant,
> validaciones—, **no** con tests de relleno ni `# pragma: no cover` para esquivar el número.

Cuando dos encargos van en paralelo y uno cambia un contrato que el otro consume, **dilo en los
dos**. Al de interfaz: «no toques el consumo de `unread_count`; el backend está cambiando ese
contrato ahora y el orquestador te encargará la adaptación después». Al de datos: «esto cambia el
contrato: descríbelo con precisión en tu informe, porque es otro subagente quien lo adapta».

## Ejemplo 3 — Continuar un agente (`SendMessage`)

Para la segunda vuelta no lances un agente nuevo: reanuda el que ya tiene el contexto. Abre
reconociendo lo que ya está bien, para que no lo rehaga.

> QA pasó tu trabajo: listo para entregar, los 12 puntos verificados uno por uno. Quedan tres
> cosas pequeñas. Mismas reglas: no commitees, no cambies de rama. **Tu trabajo anterior está
> verificado y lo doy por bueno; no lo rehagas.**
>
> ### 1. `.env.example` contradice la lista blanca que acabas de documentar
>
> El comentario omite `contacted_whatsapp`. Quien lea solo el `.env.example` concluirá que el
> comportamiento es un bug y lo «arreglará» de vuelta.

Úsalo también para resolver una decisión que el agente dejó abierta, dándole el razonamiento
completo y no solo el veredicto:

> Planteé tu decisión al cliente y eligió tu lectura, con un matiz: **`contacted_whatsapp` sí,
> `urgent_in_person` no.** El razonamiento que acepta tu objeción: `contacted_whatsapp` es un
> caso abierto con médico asignado y es el paciente al que el médico tuvo que dar su número
> personal, o sea quien más necesita el buzón.
>
> Esto es una desviación consciente de CA2.2. No la dejes muda: anótala en `spec.md` CA2.2 y
> CA4.5 con la fecha y el motivo.

## Cuántos agentes

La regla anti-abanico de `bost.md` manda: trabajo sobre `specs/**`, **un** solo subagente; rutas
derivadas, **tres como máximo**; **cero** subagentes de reconocimiento. Añadidos de la práctica:

- **Lee tú los documentos** antes de encargar, e inyecta lo relevante. Un explorador que te
  resume lo que ya podías leer cuesta un agente y pierde matices.
- **Un repo, un agente.** Dos repos en paralelo funciona porque las zonas no se solapan.
- **Datos antes que interfaz** cuando la interfaz consume esos datos; en paralelo si son
  independientes. Si parte del trabajo de interfaz no depende del contrato, lánzala ya y deja
  la adaptación para la segunda vuelta del mismo agente.
- **Máximo tres vueltas de corrección.** Si persiste, informa al usuario con el detalle en vez
  de seguir iterando.

## Qué revisar en lo que devuelven

Un informe de subagente **no es evidencia**. Comprueba tú, leyendo y sin editar:

- **Alcance:** `git diff --stat HEAD` por repo. ¿Algún archivo fuera de lo encargado? Si se
  salió, ¿lo declaró y lo justificó?
- **Lo excluido sigue excluido:** `grep` de lo que no debía construirse.
- **Las cifras:** que el `N passed` y la cobertura por módulo sean reproducibles.
- **Lo que afirma vs. lo que hace.** El fallo más común no es código roto: es un informe o un
  comentario que dice más de lo demostrable. En esta sesión, un agente atribuyó `role="img"` a
  un chip que no lo tenía, otro midió un contraste contra el fondo equivocado, y un tercero
  describió en pasado un estado que el repositorio nunca tuvo. Nada de eso rompe el build y todo
  desinforma a quien venga después. Mándalo corregir: el módulo ya sufrió documentación que no
  coincidía con el código.

## El error que hay que vigilar

Que la documentación se ajuste al código en vez de lo contrario. En este módulo pasó: `spec.md`,
`plan.md` y `todo.md` del repo frontend se reescribieron en el mismo cambio sin commitear que el
código, añadiendo a posteriori lo que el código ya hacía. Eso invalida esos documentos como vara
de medir.

Antídotos, los dos barato:

- **Commitea antes de corregir.** Un commit de respaldo convierte los arreglos en un diff
  revisable y hace visible qué se cambió después de medir.
- **Toda desviación de la spec se anota en la spec**, con fecha y motivo, y con una advertencia
  de no «corregirla» de vuelta. Si una decisión del cliente contradice un criterio, la spec se
  actualiza; nunca se deja el código divergente en silencio.
