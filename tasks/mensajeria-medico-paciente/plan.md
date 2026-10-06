# Implementation Plan: Mensajería médico ↔ paciente

> Spec: [`spec.md`](./spec.md) · Checklist: [`todo.md`](./todo.md) · Contexto: `.knowledge/mensajeria.md`
> Horas estimadas a 12 USD/h (Workana). Fase 1 ≈ 20 h, Fase 2 ≈ 20 h, de las cuales la parte de
> UI (5 h + 3 h) se ejecuta en el repo `medicos-por-venezuela` y se reporta desde su `todo.md`.

## Overview

No hay que inventar la conversación: `messages` ya existe cifrada y cerrada por RLS, el correo
y el SSE ya funcionan, y el token de consulta ya resuelve al paciente anónimo. El trabajo es
**darle superficie** (servicio + endpoints + buzón) y, en la segunda fase, **un conector** con la
API oficial de Meta que respete consentimiento, ventana de 24 h e idempotencia.

## Architecture Decisions

**D1. Hilo = consulta; no hay tabla de hilos.** `messages.consultation_id` ya lo es. Una tabla
`threads` duplicaría `consultations` y obligaría a sincronizar estados. El buzón se calcula con una
agregación sobre `messages` (índice `(consultation_id, sent_at, id)`).

**D2. Grants del cuerpo por `clinical_access`, no por RBAC.** El permiso `messages.read` autoriza la
*acción* (ver que hay hilo); la *lectura del cuerpo* la decide el grant de pertenencia igual que
`chief_complaint`. Así el admin gestiona sin leer, coherente con 2026-09-23.

**D3. El paciente entra por pertenencia o token, nunca por permiso.** Reutiliza
`consultation_token` (TTL 24 h): el correo de aviso lleva un token fresco. Un paciente anónimo
no necesita cuenta para responder.

**D4. Tiempo real por SSE, sin abrir tablas.** Se descarta `postgres_changes` sobre `messages`
porque exigiría SELECT a `authenticated` en una tabla clínica. El SSE emite ids y contadores; el
cliente refetch por REST (mismo criterio que la señal mínima del panel).

**D5. Un mensaje, una fila; el envío por WhatsApp es un atributo del mensaje.** `channel`,
`wa_message_id`, `delivery_status` y `error_code` viven en la fila del mensaje del médico. No se
crea una fila `system` por cada intento: el estado de entrega es un campo, no un mensaje.

**D6. Enrutado entrante a la consulta vigente más reciente del paciente.** Un paciente puede tener
varias consultas; el teléfono no identifica el hilo. Se elige la consulta abierta (o cerrada
dentro de la ventana) más reciente, siguiendo la cadena. Si hay más de una abierta, gana la de
`opened_at` más reciente y se registra `routing_note`. Lo que no enruta va a
`whatsapp_unrouted_messages` con aviso a operación: nunca se pierde en silencio.

**D7. Ventana de 24 h decidida en el servicio, no en el router.** `services/whatsapp.py` consulta el
último `patient_to_doctor` por WhatsApp del hilo; si es de hace < 24 h envía texto, si no envía
plantilla. Fuera de la ventana el cuerpo **no** sale por WhatsApp; se lee en la web.

**D8. Conector no-op sin credenciales.** Igual que Mailtrap: sin `WHATSAPP_ACCESS_TOKEN` el envío
devuelve `False` con warning. Los tests nunca llaman a Meta; se mockea `httpx.AsyncClient`.

**D9. Orden de entrega que deja `main` desplegable en cada PR.** Migración → modelo/esquemas →
servicio con tests → router con tests → avisos → SSE → UI. Fase 2 igual: config y cliente →
consentimiento → webhook → enrutado → ventana/plantilla → UI → puesta en producción.

**D10. Lo que NO se toca.** Jitsi, el claim atómico, la cola por especialidad, `registration_mail`,
`lib/firebase.ts`. Si una tarea descubre que hace falta tocarlos, se vuelve a esta spec antes de
generar.

**D11. Adjuntos clínicos con acceso protegido y presencia asimétrica.** Subida y descarga de archivos
(PDF e imágenes JPG/PNG/WEBP; GIF estrictamente bloqueado con 422) tanto para médico como para paciente,
almacenados en bucket privado (`chat-attachments`) y servidos con grant clínico y auditoría (`READ_CLINICAL_DATA`).
El paciente **no** puede ver el estado en línea del médico; solo el médico tratante visualiza si el paciente está en línea.

## Fases y estimación

### Fase 0 — Revisión y acuerdo (1–2 h, ofrecidas sin costo)

Lectura de ambos repos, esta spec, `.knowledge/mensajeria.md`, videollamada del 30-sep. Salida:
P1–P7 respondidas o marcadas como bloqueantes; horas por fase confirmadas al cliente.

### Fase 1 — Buzón web (≈ 20 h)

| Tarea | Horas | Repo |
|---|---|---|
| 1.1 Migración `mensajeria_hilos` (columnas `messages`, tabla `message_attachments`, índices, permisos) | 2 | API |
| 1.2 `Message` y `MessageAttachment` ampliados + `schemas/message.py` | 1 | API |
| 1.3 `services/messaging.py`: grants, enviar, listar, marcar leído, buzón, contadores, adjuntos y presencia asimétrica + tests | 5 | API |
| 1.4 `routers/messages.py` (R2–R5, R7, R15: upload/download adjuntos) + tests de router y concurrencia | 3 | API |
| 1.5 Avisos: `message_received`, correos médico/paciente con debounce + tests | 2 | API |
| 1.6 SSE: evento `message` en la sala + `/inbox/stream` + tests | 2 | API |
| 1.7 UI: `lib/messages.ts`, bloque en el detalle, buzón con presencia del paciente, `/mi-caso` (sin presencia del médico), subida PDF/imágenes, sala de espera | 5 | Front |
| **Total** | **20** | |

Entregable verificable: el médico escribe en el detalle, el paciente lo ve en `/mi-caso` o en la
sala por token y responde, el médico lo ve en el buzón sin recargar, y ambos reciben correo.

### Fase 2 — Puente WhatsApp (≈ 20 h)

| Tarea | Horas | Repo |
|---|---|---|
| 2.1 `Settings` WhatsApp + `services/whatsapp.py` (texto, plantilla, reintento) + tests con mock | 4 | API |
| 2.2 Consentimiento: migración, `PatientCreate`, endpoints, ventana de aviso en registro | 3 | API 2 / Front 1 |
| 2.3 Webhook: verificación, firma, parseo, idempotencia, `statuses` + tests | 5 | API |
| 2.4 Enrutado entrante, no enrutados, aviso a operación + tests | 3 | API |
| 2.5 Ventana de 24 h, plantilla, respaldo por correo + tests | 2 | API |
| 2.6 UI: estados de entrega, consentimiento, privacidad, E2E | 2 | Front |
| 2.7 Producción: app de Meta, número, plantilla aprobada, webhook público tras Caddy, prueba real | 1 | Infra |
| **Total** | **20** | |

Entregable verificable: el médico escribe en la web, llega al WhatsApp del paciente desde el
número de la plataforma, el paciente responde y aparece en el buzón con estado de entrega.

## Riesgos

- **Aprobación de Meta** (cuenta Business, número, plantilla) tarda días: 2.7 se inicia el primer
  día de la Fase 2 en paralelo, no al final. Si P2 es «no tenemos nada», Fase 2 arranca contra el
  número de prueba de Meta Developer.
- **Mailtrap es producción**: cualquier prueba local con datos del backup envía correos reales si
  falta `MAILTRAP_INBOX_ID`. Antes de 1.5, fijar el inbox de sandbox (riesgo R1 heredado).
- **La suite no corre en CI** (solo `--collect-only`): la verificación es local y se adjunta al PR.
- **Otro desarrollador activo** (Leonardo Alvarado, todos los merges): avisar por el canal del
  cliente qué archivos toca cada fase para evitar conflictos en `clinical.py`, `patients.py`,
  `notifications.py` y `waiting_room.py`.
