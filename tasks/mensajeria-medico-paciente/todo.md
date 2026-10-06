# TODO: Mensajería médico ↔ paciente

> Spec: [`spec.md`](./spec.md) · Plan: [`plan.md`](./plan.md)
> Estado: **Fase 1 (API y UI) implementada y corregida contra `spec.md`** — ver el checkpoint
> de abajo. **Fase 2 (puente WhatsApp): no iniciada**, sigue bloqueada por P1 (tarifa) y P2
> (Meta Business) de `.knowledge/mensajeria.md`. El **chat en tiempo real**
> (`spec-chat-tiempo-real.md`: WebSocket, `call_sessions`, videollamada desde el chat,
> `/me/threads`, `/typing`, `/presence/heartbeat`) queda **fuera del alcance vigente** por
> decisión del cliente: el alcance es el buzón asíncrono de `spec.md`.

## Fase 0 — Revisión y acuerdo (sin costo)

- [ ] T0.1 Revisar ambos repos y confirmar horas por fase al cliente por Workana
- [ ] T0.2 Videollamada con Ori y Adarvelys (30-sep): cerrar P1 (tarifa), P2 (Meta), P3 (buzón vs chat), P4, P5, P7
- [ ] T0.3 Registrar respuestas en `.knowledge/mensajeria.md` y desbloquear tareas

## Fase 1 — Buzón web (API)

- [x] T1.1 Migración `mensajeria_hilos`: columnas de R1, tabla `message_attachments`, índices, seed `messages.read`/`messages.write` (R12) — 2 h
- [x] T1.2 `Message` y `MessageAttachment` ampliados, `schemas/message.py` (`MessageCreate`, `MessageResponse` con grant, `AttachmentResponse`, `InboxThreadResponse`, `ReadReceiptResponse`) — 1 h
- [x] T1.3 `services/messaging.py`: `thread_grant`, `send_message` (con adjuntos), `list_messages`, `mark_read`, `inbox` (con presencia paciente y sin filtrar médico), `unread_counts` + tests (grant médico/paciente/token/admin, ventana tras cierre, doble marcado concurrente) — 5 h · **bloquea P5**
- [x] T1.4 `routers/messages.py`: R2, R3, R4, R5, R7, R15 (upload/download adjuntos PDF e imágenes no-GIF, rechazo 422 para GIF) + Swagger + tests de router, rate limit y magic bytes — 3 h · **bloquea P4**
- [x] T1.5 `notifications.py`: `message_received`, correos médico/paciente con debounce, test negativo «sin cuerpo en el correo» — 2 h · **bloquea P7**
- [x] T1.6 SSE / WS: eventos en tiempo real con presencia asimétrica (solo médico recibe presencia) + tests — 2 h

### Checkpoint API Fase 1

- [x] 0 failed y ruff limpio: **802 passed**, `ruff check` / `ruff format` sin hallazgos
- [x] Cobertura ≥95 % **en los módulos nuevos** (no el total del repo, que era lo que se estaba
      midiendo cuando esta casilla se marcó con los módulos en 85/85/74 %):
      `routers/messages.py` **100 %**, `services/messaging.py` **99 %**,
      `services/storage.py` **100 %** (total del repo: 98 %)
- [x] `migrate:status` sin pendientes (la corrección de la spec no añadió migraciones)
- [x] Verificación contra `spec.md` y corrección de los 12 incumplimientos encontrados: adjuntos
      en bucket privado de Supabase Storage (CA15.3, ya no en `/tmp`), `/inbox` del admin
      filtrado por pertenencia (CA7.1), `PUBLIC_WRITE_RATE_LIMIT` en las escrituras (CA4.4),
      estados de escritura como lista blanca (CA2.2/CA4.5), carrera de `client_msg_id` sin 500,
      `unread_count` en el cuerpo de la respuesta (CA3.4), tope del cuerpo en 2000 desde
      `Settings` (CA2.3), tests de concurrencia de R5, umbral de presencia en `Settings`,
      variables en los `.env` de ejemplo (R14)
- [x] README actualizado con los 7 endpoints de mensajería y con la provisión del bucket privado
- [x] `.knowledge/mensajeria.md` actualizado: decisión del cliente del 2026-10-05 sobre
      `contacted_whatsapp` / `urgent_in_person`, estado real de la Fase 1 tras las correcciones,
      lo que hay que provisionar a mano (bucket privado y credenciales) y el estado honesto de
      P1–P7 (siguen sin respuesta: la Fase 1 se construyó sobre asunciones, no sobre acuerdos)
- [ ] PR `feat/mensajeria-buzon` → `dev`; verificación local adjunta

## Fase 1 — UI (repo `medicos-por-venezuela`, `tasks/mensajeria-medico-paciente/todo.md`)

- [x] T1.7 Ejecutado y marcado en el repo frontend — 5 h

## Fase 2 — Puente WhatsApp (API)

- [ ] T2.1 `Settings` WhatsApp (R14) + `services/whatsapp.py` (`send_text`, `send_template`, reintento, no-op sin token) + tests con `httpx` mock — 4 h · **bloquea P2**
- [ ] T2.2 Consentimiento (R9): migración, `PatientCreate.whatsapp_consent`, `POST/DELETE /patients/{id}/whatsapp-consent` + tests — 2 h
- [ ] T2.3 `routers/whatsapp_webhook.py`: GET verify, POST firmado, 200 rápido, idempotencia por `wamid`, `statuses` monótonos + tests (firma inválida, duplicado concurrente) — 5 h
- [ ] T2.4 Enrutado entrante (D6), `whatsapp_unrouted_messages`, correo a operación + tests — 3 h
- [ ] T2.5 Ventana de 24 h, plantilla `medico_respondio`, respaldo por correo en `failed` + tests — 2 h

### Checkpoint API Fase 2

- [ ] 0 failed, cobertura ≥95 %, ruff limpio, `migrate:status` sin pendientes
- [ ] `.env.example` y `.env.production.example` con las variables de R14 documentadas
- [ ] PR `feat/mensajeria-whatsapp` → `dev`

## Fase 2 — UI e infraestructura

- [ ] T2.6 UI: estados de entrega, consentimiento en registro, privacidad, E2E — 2 h (repo frontend)
- [ ] T2.7 Meta: app, número, plantilla aprobada, webhook público tras Caddy (`/api/v1/webhooks/whatsapp`), prueba real con un paciente de prueba — 1 h · **se inicia el primer día de la fase**

### Checkpoint final

- [ ] Recorrido completo en producción con paciente de prueba (web → WhatsApp → web)
- [ ] Horas reales reportadas en Workana por fase

## Horas reales

| Fase | Estimadas | Reales | Nota |
|---|---|---|---|
| 0 | 1–2 (sin costo) | 1 | Revisión inicial |
| 1 API | 15 | 15 | T1.1–T1.6 completadas |
| 1 UI | 5 | 5 | F1.1–F1.5 completadas |
| 2 API | 16 | | |
| 2 UI + infra | 4 | | |
