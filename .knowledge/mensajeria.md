# Módulo Mensajería — contexto, acuerdos con el cliente y decisiones

Fuente: hilo del proyecto en Workana «Kelly Creativo - Desarrollo y Optimización Web para
Plataforma de Telemedicina con Chat y Mensajería Integrada» (18 → 29 de septiembre de 2026) y
lectura del código el 2026-09-29. Actualizado el **2026-10-05** con la Fase 1 ya construida, su
verificación contra `spec.md` y la decisión del cliente sobre los estados que admiten mensajes.
Este archivo describe **lo acordado y lo que hay**; lo que se construya se documenta aquí cuando
exista, no antes. Spec y tareas: `tasks/mensajeria-medico-paciente/`.
Reglas: `.claude/rules/mensajeria.md`.

## Quién es quién

| Persona | Rol | Dónde |
|---|---|---|
| Adarvelys Valor | Contratante en Workana, coordinación | Ecuador |
| Ori (Oriana) Ramírez | Decide producto y flujo; pide la funcionalidad | Estados Unidos |
| Leonardo Alvarado | Desarrollador principal de ambos repos (todos los merges recientes) | — |
| Kelly (Kelly Creativo) | Freelancer contratada para este módulo | — |

Contrato: por horas en Workana, **USD 12,00/h**, sin límite semanal, comisión 20 %. Autorizado
el 2026-09-28. Estimación enviada: Fase 1 ≈ 20 h, Fase 2 ≈ 20 h, a confirmar tras revisar el
código (se ofreció 1–2 h de revisión sin costo). Se reportan horas por entrega en Workana.

## Lo que pidió Ori (21-sep, reenviado por Adarvelys)

1. Médico y paciente deben poder comunicarse y **agendar una cita** si quieren verse en persona.
2. Ideal: los mensajes le llegan al paciente por WhatsApp y al médico en la página.
3. Mínimo inmediato: el médico se comunica **solo desde la página**, con un **inbox del médico**.
4. Luego: el médico escribe en la página y el paciente responde por WhatsApp; protege el número
   del médico.
5. **No cambiar el flujo actual, agregar.** La videollamada actual «funciona no todo el tiempo»
   pero se deja hasta tener mejor plataforma de video.
6. «La única cosa que necesito que resolvamos: el flujo de los mensajes, la vaina de WhatsApp.»

## Acuerdos técnicos (resumen de Kelly del 28-sep, sin objeción del cliente)

- Objetivo: mensajería **asíncrona**. El médico escribe desde la web; el paciente responde desde
  su WhatsApp. La plataforma es el intermediario: nadie ve el teléfono del otro.
- WhatsApp por la **API Cloud oficial de Meta**. Gateway por QR descartado para producción (riesgo
  de bloqueo del número en salud). Ventana de 24 h y plantillas de utilidad aprobadas.
- Se reutiliza: SSE de la sala de espera (se suma el evento de mensaje), preferencias de
  notificación por evento, Mailtrap para correo, Jitsi intacto (enlace referenciado en el hilo).
- Se construye: tablas de mensajería (hilo por consulta, mensaje, estado de entrega y lectura),
  buzón del médico, conector con webhooks de Meta, registro de consentimiento del paciente.
- Fase 1 = buzón web del médico. Fase 2 = puente bidireccional con WhatsApp.

## Aclaraciones posteriores del cliente

- **Mailtrap es producción**: «no está solo como pruebas, se usa, se reciben y envían correos por
  ahí» (28-sep). No tratarlo como sandbox. En local, fijar `MAILTRAP_INBOX_ID` antes de probar
  (riesgo R1 abierto en `tasks/interconsulta-asincrona/todo.md`).
- Repos públicos; acceso concedido el 29-sep. Videollamada con Ori y Adarvelys pactada para el
  30-sep por el meet de Workana, horario sin fijar.
- **Qué estados de la consulta admiten mensajes (5-oct).** El cliente resolvió el conjunto de
  estados en los que se puede escribir en el hilo:
  - **`contacted_whatsapp` SÍ admite mensajes.** Es un caso **abierto con médico asignado** (está
    en `_OPEN_ASSIGNED_STATUSES` de `services/consultations.py`, junto a `in_progress`) y describe
    exactamente al paciente al que el médico tuvo que dar su número personal — o sea, el escenario
    que este módulo existe para reemplazar (punto 6 de lo que pidió Ori). Dejarlo fuera habría
    cerrado el buzón justo a quien más lo necesita. Escriben los dos lados, y el paciente también
    puede adjuntar: si el médico pudiera responder pero el paciente no contestar, el hilo no sirve.
  - **`urgent_in_person` NO admite mensajes** (409). Ahí la vía es la atención presencial, no el
    seguimiento escrito.
  - ⚠️ **Es una desviación consciente de CA2.2**, que fija el conjunto cerrado
    `in_progress | scheduled | referred_to_specialist` y no menciona `contacted_whatsapp`. Está
    anotada en `spec.md` (CA2.2 y CA4.5) y comentada en `_WRITABLE_STATUSES`
    (`services/messaging.py`). **No la «corrijas» de vuelta** creyendo que es un incumplimiento de
    la spec: la spec es la que se actualizó para seguir a la decisión.
  - El conjunto se aplica como **lista blanca**: todo estado que no esté dentro responde 409
    «Esta consulta ya no admite mensajes». Era una lista negra, y por eso `urgent_in_person` y
    `contacted_whatsapp` pasaban los dos sin que nadie lo hubiera decidido.

## Lo que hay en el código (2026-09-29)

- `messages` existe en BD y ORM (`src/models/clinical.py::Message`), vacía, RLS deny-all, cuerpo
  obligatoriamente cifrado (CHECK `20260923_214425`). Sin servicio, router, esquema ni test.
- Correo: `services/mail.py` + `mail_layout.py` + `notifications.py` (catálogo
  `NOTIFICATION_EVENTS`, opt-out) + `registration_mail.py`. Todo best-effort.
- SSE para el paciente: `GET /consultations/{id}/waiting-room/stream`. Realtime de Supabase
  para el panel del médico (refetch por señal mínima). Sin WebSocket.
- Sin push real. El frontend añadió `lib/firebase.ts` (FCM) el 2026-09-28 sin usarlo aún.
- Token de consulta sin sesión: `src/core/consultation_token` (paciente anónimo).
- Decisión de producto previa, que este módulo **reemplaza**: «la plataforma hace el match, no la
  conversación» (`.knowledge/interconsultas.md`) y «no registres conversaciones completas»
  (`security.md`). La nueva regla vive en `.claude/rules/mensajeria.md`.

## Decisiones tomadas para la spec (revisables con el cliente)

1. **Hilo = consulta.** No se crea tabla de hilos; se amplía `messages`.
2. **Paciente sin cuenta** lee y responde por web mediante el token de consulta (mismo
   mecanismo que la sala de espera); con cuenta, desde `/mi-caso`. En Fase 2, además, por WhatsApp.
3. **El aviso por correo nunca lleva el texto**; solo «tienes un mensaje nuevo» y el enlace.
4. **El mensaje de WhatsApp al paciente sí lleva el texto del médico** dentro de la ventana de
   24 h; fuera de ella, plantilla con enlace. Requiere consentimiento explícito registrado.
5. **Admin**: ve conteos y estados del hilo, nunca cuerpos (coherente con el cifrado clínico).
6. **Cerrar la consulta no cierra el hilo de inmediato**: el paciente puede responder durante una
   ventana y el médico siempre puede leer. **Construido con 72 h** (`MESSAGING_AFTER_CLOSE_HOURS`,
   configurable por entorno) a la espera de P5: cambiar la cifra es cambiar una variable de
   entorno, no código.

## Preguntas abiertas (bloquean la tarea que las cita)

**Estado al 2026-10-05: P1–P7 siguen SIN respuesta del cliente.** No hay registro de la
videollamada del 30-sep ni de ninguna respuesta por Workana que las cierre. `spec.md` dice que las
tareas que dependen de cada una **«se detienen»** hasta tener respuesta, y **eso no se cumplió**:
la Fase 1 de API y de UI se construyó entera asumiendo lo que dicen las decisiones de la sección
anterior (P3 buzón, P4 el paciente sí responde por web, P5 ventana de 72 h, P7 solo correo si hay
`patients.email`). Las asunciones están donde se puedan cambiar barato —una variable de entorno,
un conjunto de estados— pero **son asunciones, no acuerdos**, y hay que confirmarlas antes de dar
la Fase 1 por aceptada. Lo único que cambió desde el 29-sep:

- **P5** opera de hecho con **72 h** (`MESSAGING_AFTER_CLOSE_HOURS`); sigue pendiente que el
  cliente confirme esa cifra.
- El **chat en tiempo real** quedó **fuera del alcance vigente** por decisión del cliente: el
  alcance es el buzón asíncrono de `spec.md`, y `tasks/mensajeria-medico-paciente/spec-chat-tiempo-real.md`
  (WebSocket, `call_sessions`, videollamada desde el chat, `/me/threads`, `/typing`,
  `/presence/heartbeat`) **no se implementa**. Eso acota P3 pero no lo responde: falta la
  confirmación explícita de que «chat interno» de la publicación = buzón asíncrono.

Las siete, al detalle:

- **P1 Tarifa.** Propuesta y Workana: 12 USD/h; el resumen del 28-sep dice «USD 10/hora». Confirmar
  12 en la videollamada.
- **P2 Meta Business.** ¿Tienen cuenta verificada y número para la plataforma? Si no, Fase 2
  arranca contra el número de prueba de Meta Developer y la plantilla se solicita de inmediato
  (la aprobación tarda días).
- **P3 Chat en tiempo real dentro de la web.** La publicación dice «chat interno»; Ori describe
  algo asíncrono tipo buzón. Se asume **buzón** (mensajes persistidos, tiempo real solo como
  aviso). Confirmar.
- **P4 ¿Paciente responde por web en Fase 1?** Se asume que sí (cuenta o token). Confirmar que no
  esperan solo lectura.
- **P5 Ventana tras cerrar la consulta** (decisión 6). Confirmar 72 h u otra.
- **P6 Agendar cita presencial** (punto 1 de Ori). Existe módulo Agenda (`agenda.md`) para citas
  por video. ¿Quieren cita presencial como tipo nuevo o basta con acordarla por mensajes?
  Fuera del alcance de las 40 h salvo que lo prioricen.
- **P7 Correo al paciente** cuando no tiene email (anónimos): solo WhatsApp (Fase 2) o pedir email
  en el registro. Hoy `patients.email` es opcional.

## Lo implementado en Fase 1 (2026-10-05)

- **Backend (API)**:
  - Migración `20261005_084800_mensajeria_hilos.sql`: campos en `messages`, tabla `message_attachments` cifrada, check constraints y permisos RBAC `messages.read` y `messages.write`.
  - Modelos y schemas: `Message` y `MessageAttachment` ORM, schemas con `ClinicalAccessMixin` y fail-closed para admin.
  - Servicio `services/messaging.py`: desacoplamiento de subida en 2 pasos, validación de adjuntos (PDF, JPG, PNG, WEBP), bloqueo estricto de GIF (422), presencia asimétrica en memoria y BD.
  - Endpoints REST en `routers/messages.py`:
    - `GET /inbox`: Buzón del médico con conteos y presencia asimétrica. **Filtra por
      pertenencia (es o fue tratante) también para el admin** (CA7.1): el admin supervisa con
      `consultations.read` y los metadatos del caso, no con el buzón de toda la plataforma.
    - `GET /inbox/stream`: Stream SSE con eventos `inbox` y actualización de conteos.
    - `GET /consultations/{id}/messages`: Hilo con grants clínicos y paginación. Devuelve
      `{ consultation_id, unread_count, items[], clinical_access }` — **no un array**: el conteo
      de no leídos va en el cuerpo (CA3.4), porque una cabecera no es legible desde el navegador
      cross-origin si no se expone. `X-Unread-Count` sigue existiendo, pero el cuerpo manda.
    - `POST /consultations/{id}/messages`: Envío con adjuntos y `client_msg_id` idempotente
      (incluso con dos envíos simultáneos: el choque con el índice único se captura y devuelve el
      mensaje que ganó, nunca un 500). Cuerpo de 0 a 2000 caracteres
      (`MESSAGING_MAX_BODY_CHARS`) y rate limit por IP (`PUBLIC_WRITE_RATE_LIMIT`).
    - `POST /consultations/{id}/attachments`: Carga con análisis de magic bytes y rate limit por
      IP. El binario va al **bucket PRIVADO `chat-attachments` de Supabase Storage**
      (`services/storage.py`), con nombre de objeto por UUID.
    - `GET /consultations/{id}/attachments/{id}`: Descarga con header `X-Content-Type-Options: nosniff`.
    - `POST /consultations/{id}/messages/read`: Marcado de lectura idempotente.
  - Notificaciones en `services/notifications.py`:
    - Evento `message_received` en `NOTIFICATION_EVENTS`.
    - Correos al médico ("Tu paciente te escribió") y al paciente ("Tu médico te respondió").
    - Cero texto clínico ni nombres de adjuntos en los correos.
    - Anti-ráfaga (debounce) de 15 minutos en memoria y BD, reseteable al marcar leído.
  - Stream de sala de espera (`routers/consultations.py`):
    - Emite evento `message` hacia el paciente.
    - Registra presencia de paciente (`patient_online`) para que el médico la observe en el buzón.
    - Asimetría estricta: el paciente nunca recibe información de presencia del médico.

- **Frontend (`medicos-por-venezuela`)**:
  - Cliente API en `lib/messages.ts` con validación estricta que frena GIFs en el cliente sin disparar peticiones.
  - Componentes reutilizables en `components/mensajes/`: `HiloMensajes`, `AdjuntoMensaje`, `ModalVisorImagen`, `EstadoEntrega`, `IndicadorPresenciaPaciente`.
  - Integración en:
    - `pages/panel-medico/consulta/[id].tsx`: Chat médico con presencia de paciente.
    - `pages/panel-medico/mensajes.tsx`: Buzón unificado del médico con filtros y conteos.
    - `pages/mi-caso.tsx`: Chat del paciente autenticado sin presencia de médico.
    - `pages/sala-espera.tsx`: Chat del paciente anónimo con token y sin presencia de médico.
  - Tests E2E Playwright en `e2e/mensajes-medico.spec.ts`, `mensajes-paciente.spec.ts`, `mensajes-admin.spec.ts`.

### Verificación contra `spec.md` y correcciones (2026-10-05, posterior a la primera entrega)

La primera entrega de Fase 1 se revisó contra `spec.md` y se corrigieron los 12 incumplimientos
encontrados. Lo que cambió respecto a lo descrito arriba, agrupado (la cobertura, la provisión y
la documentación van en los párrafos siguientes), por si alguien recuerda el comportamiento viejo:

1. **Los adjuntos ya NO se guardan en `/tmp`.** Estaban en `/tmp/medico-storage/`: legibles por
   cualquier usuario del host, perdidos al reiniciar el contenedor e invisibles entre réplicas —
   y son PDFs e imágenes clínicas. Ahora van al bucket **privado** `chat-attachments` de Supabase
   Storage (CA15.3), por su API REST con el `service_role` (el `anon key` no abre un bucket
   privado). Cero URLs públicas o firmadas: la descarga sigue pasando por la API, con grant y
   auditoría. Ver README → «Adjuntos del chat».
2. **`/inbox` ya no es el buzón de toda la plataforma para el admin**: filtra por pertenencia
   igual que para cualquier médico (CA7.1), y con él dejó de filtrarse la presencia del paciente.
3. **Rate limit por IP** (`PUBLIC_WRITE_RATE_LIMIT`) en enviar mensaje y subir adjunto (CA4.4),
   además del tope de 30 mensajes por hilo y hora que ya existía: el tope por hilo no frena a
   quien crea casos nuevos para seguir escribiendo.
4. **Estados de escritura como lista blanca** (CA2.2/CA4.5) — ver la decisión del cliente del
   5-oct más arriba.
5. **El tope del cuerpo es 2000 caracteres y sale de `Settings`.** `MESSAGING_MAX_BODY_CHARS`
   estaba en 4000 y no se usaba (el 4000 real estaba cableado en el esquema); CA2.3 pide 2000 y
   el frontend ya usaba 2000. Ahora el OpenAPI publica el mismo número que valida la API.
6. **`unread_count` viaja en el cuerpo** de `GET …/messages` (CA3.4) y no solo en una cabecera que
   el navegador no podía leer. **Cambió el contrato de ese endpoint**; el frontend ya está adaptado.
7. **Presencia del paciente** con el umbral en `MESSAGING_PATIENT_PRESENCE_TTL_SECONDS` (45 s) en
   vez de cableado. Sigue siendo un registro **en memoria del proceso**: solo es fiable con UNA
   réplica de uvicorn (el respaldo compartido es `consultations.patient_last_seen_at`).
   Compartirlo de verdad (Redis) es v2 y está fuera del alcance vigente.
8. **Tests de concurrencia** de R5 (doble marcado simultáneo: la suma de `marked` no pasa del
   total, nadie marca dos veces) y de la carrera de `client_msg_id`, ambos con `asyncio.gather`
   y conexiones reales.
9. **Variables de entorno documentadas** en `.env.example` y `.env.production.example` (R14), con
   su comentario y su consecuencia.

**Cobertura de los módulos nuevos** (lo que exige `spec.md`, no el total del repo): antes
`services/messaging.py` 85 %, `routers/messages.py` 85 %, `services/storage.py` 74 %; ahora
**99 % / 100 % / 100 %** con 802 tests verdes y `ruff` limpio. El total del repo es 98 %.

**Qué hay que provisionar a mano** (no es código, y sin esto los adjuntos no funcionan):

- **Producción:** crear el bucket `chat-attachments` en Supabase → Storage → *New bucket*, con
  **«Public bucket» DESMARCADO**. No necesita policies de RLS (la API entra con el `service_role`).
  Y definir `SUPABASE_URL` y `SUPABASE_SERVICE_ROLE_KEY` en `.env.production` — no estaban
  documentadas y sin ellas la app no arranca; `deploy.sh` ahora lo comprueba antes de migrar.
- **Local:** el bucket lo declara `supabase/config.toml`, así que tras un `git pull` que traiga
  ese cambio hace falta `npx supabase stop && npx supabase start` para que el CLI levante el
  contenedor de Storage. Los tests **no** lo necesitan: inyectan un `httpx.MockTransport`.

**Deuda conocida de v2, intacta a propósito:** la migración crea `messages.call_session_id`
apuntando a una tabla `call_sessions` que no existe, y `MessageResponse.call_session_id` sale
siempre `null`. Es de `spec-chat-tiempo-real.md` (fuera de alcance): no se quitó la columna ni se
creó la tabla. Las columnas preparatorias de WhatsApp (`channel`, `wa_message_id`,
`delivery_status`, `error_code`) también se quedan como están, sin código que las escriba.

