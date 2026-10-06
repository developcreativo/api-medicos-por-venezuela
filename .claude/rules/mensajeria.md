# Reglas de Mensajería (buzón médico ↔ paciente, correo y WhatsApp)

Aplican a todo código que toque `messages`, el buzón del médico, el conector de WhatsApp (API
Cloud de Meta) y los avisos por correo o push de mensajes nuevos. Complementan `security.md`;
donde choquen, manda `security.md`. Contexto y acuerdos con el cliente: `.knowledge/mensajeria.md`.

## 🧭 Modelo

- **Un hilo = una consulta.** `messages.consultation_id` es el hilo; no existe una tabla de hilos
  aparte. El paciente derivado sigue su cadena (`waiting_room.current_in_chain`): el hilo vigente
  es el de la consulta hija, y el histórico se lee por la cadena, hacia abajo, nunca hacia arriba.
- **La plataforma es el intermediario.** El médico nunca recibe el teléfono del paciente por este
  módulo y el paciente nunca ve el del médico. Ningún esquema `*Response` de mensajería expone
  `phone`, `whatsapp` ni identificadores de Meta del otro lado.
- **Canales del mensaje:** `web` (escrito en la plataforma) y `whatsapp` (entrante por webhook o
  saliente por API de Meta). `direction`: `doctor_to_patient` | `patient_to_doctor`. Solo esos.

## 🔐 Contenido

- **`messages.body` va cifrado** (`EncryptedText("messages.body")`; el CHECK de
  `20260923_214425` rechaza texto plano). Se lee como `Sealed` y sale por un esquema con
  `ClinicalAccessMixin`; sin grant sale `null`.
- **Grant de lectura del hilo:** el médico tratante (`assigned_doctor_id`) o quien lo fue en la
  cadena, el paciente dueño (`owns_patient` / token de consulta) y nadie más. **Ser admin no
  concede lectura**; el admin ve conteos y estados, no cuerpos. Toda lectura concedida se audita
  con `audit_clinical_read` (`READ_CLINICAL_DATA`, vía `messages`).
- **Escritura:** el médico solo en consultas suyas y no cerradas; el paciente solo en su propia
  consulta y solo mientras esté abierta o dentro de la ventana que defina la spec. Pertenencia
  en el servicio, en **todas** las rutas que mutan el hilo (crear, marcar leído, reenviar).
- **El cuerpo del mensaje no viaja por correo, `.ics`, Excel, logs ni SSE.** Los canales de aviso
  llevan solo "tienes un mensaje nuevo" y el enlace. La única salida del texto fuera de la
  plataforma es el mensaje de WhatsApp al paciente, y solo con consentimiento registrado.
- Excepción a `security.md` § "No registres conversaciones completas": este módulo **sí** guarda
  la conversación médico ↔ paciente, cifrada y con acceso por necesidad de saber. La regla sigue
  vigente para todo lo demás (interconsultas, notas, correos).

## 📲 WhatsApp (API Cloud de Meta)

- **Solo API oficial.** Prohibido cualquier gateway por QR o sesión de WhatsApp Web en código de
  producto, incluso como fallback. Si se quiere una demo en desarrollo, vive fuera del repo.
- **Consentimiento primero.** Ningún envío saliente sin `patients.whatsapp_consent_at` (o la
  tabla que defina la spec) registrado y sin que el paciente haya aceptado los términos. Revocar
  el consentimiento detiene los envíos de inmediato.
- **Ventana de 24 h.** Fuera de la ventana desde el último mensaje del paciente solo se envía una
  **plantilla de utilidad aprobada** ("tu médico respondió, entra aquí") con enlace tokenizado; el
  cuerpo del médico se lee en la plataforma. Dentro de la ventana se envía texto libre.
- **Webhook entrante:** verificar `X-Hub-Signature-256` con el `APP_SECRET`; responder 200
  siempre y procesar en background; **idempotente por `wamid`** (un mensaje repetido no crea dos
  filas). Mapeo `teléfono → paciente → consulta vigente`; si no hay consulta abierta, se guarda
  como no enrutado y se avisa al equipo de operación, nunca se descarta en silencio.
- **Estados de entrega** (`sent | delivered | read | failed`) se actualizan desde el webhook de
  `statuses` por `wamid`, con escritura condicional: nunca retroceder un estado.
- **Nada de PII en logs del conector**: se loguea `wamid`, tipo de evento y código de error de
  Meta; jamás número, nombre ni cuerpo.
- Secretos (`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_PHONE_NUMBER_ID`,
  `WHATSAPP_VERIFY_TOKEN`) solo por `Settings`; sin ellos el conector es un no-op con warning,
  igual que Mailtrap.

## 🔔 Avisos

- Nuevo mensaje al médico: evento `message_received` en `NOTIFICATION_EVENTS` con los canales que
  existan de verdad (`email`, y `push` solo cuando haya Web Push real). Respeta `should_send`.
- Nuevo mensaje al paciente: WhatsApp si hay consentimiento y número; correo si hay
  `patients.email`; y siempre visible en `/mi-caso` o en la sala de espera por token.
- Correos por `services/mail.py` con `best_effort` y `BackgroundTasks`. Mailtrap es
  **producción** para este cliente (envía y recibe correos reales): no lo trates como sandbox y
  respeta `MAILTRAP_INBOX_ID` en local antes de cualquier prueba.

## ⏱️ Tiempo real

- Hacia el paciente: reutilizar el SSE de la sala de espera añadiendo el evento `message`;
  no abrir un segundo mecanismo.
- Hacia el médico: SSE propio del buzón con el mismo patrón (`poll`, `heartbeat`, `max_seconds`
  de `Settings`), o refetch disparado por Realtime sobre una señal mínima. El payload del SSE
  nunca lleva cuerpos: lleva `consultation_id`, `message_id` y contadores; el cliente refetch.
