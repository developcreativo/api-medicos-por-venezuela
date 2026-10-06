---
name: mensajeria
description: Contexto y checklist para trabajar en el módulo de mensajería médico ↔ paciente de la API (buzón del médico, hilo por consulta, conector WhatsApp API Cloud de Meta, avisos por correo). Cargar antes de tocar `messages`, `services/messaging*`, `routers/messages*`, el webhook de WhatsApp o los avisos de mensaje nuevo. Usar cuando el usuario mencione buzón, inbox, chat, mensajes, WhatsApp, Meta, plantillas o consentimiento del paciente.
---

# Mensajería médico ↔ paciente (API)

## Qué es y de dónde sale

Encargo del cliente (Workana, septiembre 2026; acuerdos completos en
`.knowledge/mensajeria.md`): el médico voluntario escribe y lee desde un **buzón en la web**;
al paciente le llega por **WhatsApp** desde el número de la plataforma y responde ahí; su
respuesta aparece en el buzón del médico. Ni uno ve el teléfono del otro. Dos fases cobradas por
horas: **Fase 1** buzón web (hilo por consulta, envío/lectura, avisos por correo, tiempo real);
**Fase 2** puente WhatsApp por la **API Cloud oficial de Meta** (nada de gateway por QR).

Spec canónica, plan y tareas: `tasks/mensajeria-medico-paciente/`. Reglas duras:
`.claude/rules/mensajeria.md`. Léelos completos antes de escribir código.

## Estado real del código (verificado 2026-09-29)

- `messages` existe en BD y como ORM (`src/models/clinical.py::Message`: `consultation_id`,
  `sender_role`, `body` cifrado, `sent_at`, `read_at`). **No hay servicio, router, esquema ni
  test.** RLS deny-all desde `20260923_134911`; CHECK de ciphertext desde `20260923_214425`.
- Correo maduro: `services/mail.py` (Mailtrap, best-effort, streams transaccional/bulk),
  `mail_layout.py`, `notifications.py` (catálogo `NOTIFICATION_EVENTS`, opt-out por usuario),
  `registration_mail.py` (frontera de PII con tests negativos).
- Tiempo real hacia el paciente: SSE `GET /consultations/{id}/waiting-room/stream`
  (`services/waiting_room.py`, poll 4 s, heartbeat 15 s, máx 300 s). Hacia el médico: Realtime de
  Supabase sobre `consultations(id, status, assigned_doctor_id)` que dispara refetch; no hay
  WebSocket.
- Push: **no existe** en la API. El frontend acaba de añadir `lib/firebase.ts` (FCM, 2026-09-28,
  otro desarrollador) sin uso todavía; no asumir que hay Web Push hasta que exista un endpoint
  que guarde tokens.
- Cifrado clínico y grants: `src/core/clinical_crypto.py`, `services/clinical_access.py`,
  `docs/cifrado-datos-clinicos.md`. Cualquier lectura del cuerpo pasa por ahí.
- Token de consulta sin sesión (paciente anónimo): `src/core/consultation_token`
  (`X-Consultation-Token`, TTL 24 h). Es la vía para que un paciente sin cuenta lea o responda.

## Dónde vive cada cosa nueva (convención acordada en el plan)

```
db/migrations/AAAAMMDD_HHMMSS_mensajeria_*.sql   columnas nuevas de messages, consentimiento, permisos
src/models/clinical.py                            ampliar Message (sender_user_id, channel, direction, delivery_status, wa_message_id)
src/schemas/message.py                            MessageCreate / MessageResponse (ClinicalAccessMixin) / InboxThreadResponse
src/services/messaging.py                         hilo, envío, lectura, grants, contadores, SSE del buzón
src/services/whatsapp.py                          cliente Meta (envío texto/plantilla), verificación de firma, parseo de webhook
src/routers/messages.py                           /consultations/{id}/messages, /inbox, /inbox/stream
src/routers/whatsapp_webhook.py                   GET/POST /webhooks/whatsapp
tests/test_messaging*.py, tests/test_whatsapp*.py
```

## Checklist antes de abrir el PR

- [ ] Permisos nuevos (`messages.read`, `messages.write`) sembrados en migración y usados con
      `require_permission`; el paciente entra por pertenencia o token, no por permiso de staff.
- [ ] Pertenencia verificada en el servicio en **todas** las rutas del hilo.
- [ ] Cuerpo cifrado; esquema de salida con grant; lectura auditada; `null` sin grant.
- [ ] Nada del cuerpo en correo, SSE, logs, Excel ni `.ics`.
- [ ] Consentimiento registrado antes de cualquier envío por WhatsApp; ventana de 24 h y plantilla
      fuera de ventana; webhook idempotente por `wamid` y con firma verificada.
- [ ] Estados de entrega con escritura condicional (nunca retroceden).
- [ ] Eventos `message_received` (médico) y aviso al paciente respetando `should_send` y
      `patients.email` opcional.
- [ ] Tests: servicio, router, concurrencia de doble envío/lectura, webhook duplicado, firma
      inválida, paciente sin consentimiento, admin sin cuerpo. Cobertura ≥95 %.
- [ ] `.knowledge/mensajeria.md` y README (endpoints) actualizados con lo que **quedó**, no con lo
      planeado. Horas reales anotadas en `tasks/mensajeria-medico-paciente/todo.md`.

## Preguntas que NO se responden solas

Si alguna sigue abierta en `tasks/mensajeria-medico-paciente/spec.md`, la tarea que depende de
ella se detiene y se pregunta al cliente por Workana. No se elige la interpretación plausible.
