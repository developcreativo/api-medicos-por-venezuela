# Verificación de correo en el registro (código de 6 dígitos)

Estado: aprobado (2026-09-21). Decisiones del equipo:

1. **Verificación real server-side**: la API exige un `email_verification_token` (firmado, atado
   al correo y al propósito) cuando el alta trae correo. Se gatea con `EMAIL_VERIFICATION_REQUIRED`
   y los tests lo apagan (como ya hacen con el rate limiter).
2. **e2e**: el endpoint de envío devuelve `debug_code` SOLO si `EMAIL_VERIFICATION_DEBUG_CODE` está
   activo y `ENVIRONMENT != production`; el arranque aborta si está activo en producción. Los e2e
   capturan el código de esa respuesta (Playwright `waitForResponse`), sin depender de Mailtrap.

## Flujo de usuario

1. Paciente/profesional completa el formulario y pulsa "Registrarse".
2. **Antes de tocar el API**, un modal confirma el correo: "¿Está seguro de registrarse con el
   correo `talcorreo@dominio.com`? Verifique su información antes de continuar". El correo va
   grande y en negritas. Botones: "Corregir" (vuelve al formulario) y "Continuar".
3. El chequeo de "correo ya registrado" que ya existe para médicos (`registration-check`) se
   mantiene tal cual, antes del modal.
4. Al continuar: **pantalla de código de 6 dígitos** (modal a pantalla completa). Muestra el correo,
   un input de 6 dígitos, "Reenviar código" (con cooldown de 60 s), la nota de revisar spam y que
   el paso es necesario para verificar el correo. El código vale **60 minutos** (dentro del rango
   30-90 pedido).
5. Verificado: se emite el token y recién ahí corre el resto del registro (Supabase `signUp` +
   `POST /doctors` o `POST /patients` + consulta), con el token en el payload.

Se saltan el modal y el código (ya hay sesión iniciada, correo probado por el login):
- Paciente con sesión (`authedPatient` en `registro-paciente.tsx`).
- Médico con cuenta a medio registrar (`incomplete` → `signInWithPassword`).

## Backend

### Migración `email_verification_codes`
Una fila por (email, purpose), con contadores; RLS habilitada sin policies (la API entra como dueña,
PostgREST no toca la tabla).

```sql
create table if not exists public.email_verification_codes (
  id uuid primary key default gen_random_uuid(),
  email text not null,                       -- normalizado a minúsculas
  purpose text not null check (purpose in ('patient','doctor')),
  code_hash text not null,                   -- HMAC-SHA256(secreto, "email:purpose:codigo")
  attempts smallint not null default 0,
  sends_count smallint not null default 0,
  window_started_at timestamptz not null default now(),
  last_sent_at timestamptz not null default now(),
  expires_at timestamptz not null,
  consumed_at timestamptz,
  created_at timestamptz not null default now()
);
create unique index if not exists uq_email_verification_email_purpose
  on public.email_verification_codes (email, purpose);
alter table public.email_verification_codes enable row level security;
```

### Config (`src/core/config.py`)

```python
EMAIL_VERIFICATION_SECRET: str = "dev-insecure-email-verification-secret-change-me"
EMAIL_VERIFICATION_CODE_TTL_MINUTES: int = 60
EMAIL_VERIFICATION_TOKEN_TTL_MINUTES: int = 15
EMAIL_VERIFICATION_MAX_ATTEMPTS: int = 5
EMAIL_VERIFICATION_RESEND_SECONDS: int = 60
EMAIL_VERIFICATION_MAX_SENDS_PER_HOUR: int = 5
EMAIL_VERIFICATION_REQUIRED: bool = True
EMAIL_VERIFICATION_DEBUG_CODE: bool = False  # dev/e2e; el arranque aborta en producción
EMAIL_VERIFICATION_SEND_RATE_LIMIT: str = "10/minute"
EMAIL_VERIFICATION_VERIFY_RATE_LIMIT: str = "20/minute"
```

`src/main.py`: aborta el arranque si `ENVIRONMENT == "production" and EMAIL_VERIFICATION_DEBUG_CODE`
(y si `EMAIL_VERIFICATION_SECRET` queda en el default, igual que `CONSULTATION_TOKEN_SECRET`).
Documentar en `.env.example` y `.env.production.example`.

### Token (`src/core/email_verification_token.py`, copia de `consultation_token.py`)

- `_TOKEN_TYPE = "email_verification"`, HS256 con `EMAIL_VERIFICATION_SECRET`.
- `issue(email, purpose) -> str`: `sub` = email normalizado, `purpose`, `typ`, `iat`, `exp`
  (`EMAIL_VERIFICATION_TOKEN_TTL_MINUTES`).
- `is_valid_for(token, email, purpose) -> bool`: valida firma, `typ`, `sub` y `purpose`.

### Servicio (`src/services/email_verification.py`)

- `hash_code(email, purpose, code)` y comparación con `hmac.compare_digest`.
- `send_code(session, *, email, purpose) -> str` (devuelve el código para que el router lo mande):
  - Normaliza el correo. Cooldown: `now - last_sent_at < RESEND_SECONDS` → `TooManyRequestsError`.
  - Ventana horaria: si `now - window_started_at >= 1 h`, reinicia; si `sends_count >= MAX` →
    `TooManyRequestsError`.
  - Código `secrets.randbelow(1_000_000)` con 6 dígitos; upsert `on conflict (email, purpose)` que
    reemplaza hash/expiración, resetea `attempts` y actualiza contadores.
- `verify_code(session, *, email, purpose, code) -> str` (token):
  - Sin fila, expirada o consumida → `UnprocessableError("El código expiró o no existe. Pide uno nuevo.")`.
  - `attempts += 1`; si supera `MAX_ATTEMPTS` → marca consumida y `TooManyRequestsError`.
  - Incorrecto → `UnprocessableError("Código incorrecto.")` (sumando el intento, commit).
  - Correcto → `consumed_at = now`, commit y devuelve `email_verification_token.issue(...)`.
- `ensure_verified(*, token, email, purpose)`: si `EMAIL_VERIFICATION_REQUIRED` y hay correo, exige
  token válido → si no, `ForbiddenError("Verifica tu correo antes de continuar.")`.
- `core/errors.py`: agregar `TooManyRequestsError` (429) y mapearla en `core/exceptions.py` si el
  manejador no la cubre genéricamente.

### Correo

- `registration_mail.py`: `build_email_verification_mail(code, ttl_minutes) -> (subject, text, html)`.
  Subject "Tu código de verificación es 123456"; HTML con el código grande (≈34px, negrita,
  `letter-spacing`), "Válido por 60 minutos", nota de spam y de que es necesario para verificar el
  correo. Usa el `mail_layout` de marca y escapa lo que haga falta.
- Router `src/routers/email_verification.py` (público, rate-limited):
  - `POST /email-verification/send` `{email, purpose}` → `{sent, expires_minutes, resend_seconds,
    debug_code?}`. Si `send_mail` devuelve False (Mailtrap sin token o caído) → 503
    (`ServiceUnavailableError` nuevo o `HTTPException`, con mensaje claro).
  - `POST /email-verification/verify` `{email, purpose, code}` → `{verification_token, expires_minutes}`.
  - `debug_code` solo cuando `EMAIL_VERIFICATION_DEBUG_CODE` y `ENVIRONMENT != "production"`.

### Enforcement en los altas

- `DoctorCreate` y `PatientCreate`: nuevo campo opcional `email_verification_token: str | None`.
- `POST /doctors` (correo obligatorio) y `POST /patients` (correo opcional: el alta anónima por API
  sin correo sigue funcionando y no exige token) validan con `ensure_verified` antes de crear.

## Frontend

- `lib/emailVerification.ts`: `sendEmailVerification(email, purpose)` y
  `verifyEmailCode(email, purpose, code)`.
- `components/ConfirmarCorreoModal.tsx`: correo grande y en negritas + los dos botones.
- `components/VerificacionCodigoModal.tsx`: input de 6 dígitos centrado, reenviar con countdown,
  nota de spam, errores; al verificar llama `onVerified(token)`.
- `registro-paciente.tsx`:
  - **Se elimina el checkbox "Conozco la especialidad que necesito"**: el select de especialidad
    queda visible siempre y opcional (sin marcar nada cae en Medicina general). La preselección de
    Psicología por `?especialidad=psicologia` sigue igual (setea la especialidad directamente).
  - `submit()`: zod → si `!authedPatient`, modal de correo → modal de código → token → flujo actual
    (`signUp`, `createPatient` con `email_verification_token`, `createConsultation`).
- `registro-medico.tsx`: después del `registration-check`, si es alta nueva → modal de correo →
  código → `signUp` + `createDoctor` con token. El camino `incomplete` no pasa por el código.
- `lib/doctors.ts` / `lib/patients.ts`: campo `email_verification_token` en los payloads.
- e2e: helper compartido que maneja confirmación + captura `debug_code` (`page.waitForResponse`) +
  rellena el código; se usa en `registro-paciente.spec.ts` y `mi-caso-videoconsulta.spec.ts`; se
  quita el checkbox del spec y se ajusta el assert de "Otra" (el select ahora siempre está).
  Para correr e2e local: `EMAIL_VERIFICATION_DEBUG_CODE=true` en `.env`.
- `changeslog.md` al terminar.

## Operación (producción)

- `.env.production` del API: `EMAIL_VERIFICATION_SECRET` (32 bytes aleatorios, propio),
  `MAILTRAP_API_TOKEN` + `MAIL_FROM_EMAIL`/`MAIL_FROM_NAME` (sin token el OTP no se puede enviar y
  el registro responde 503), `EMAIL_VERIFICATION_DEBUG_CODE=false`.
- Aplicar migración: `python artisan migrate`.
- Supabase: si la confirmación por correo de Auth está activa, el usuario recibiría además el correo
  de Supabase; conviene desactivarla para que la única verificación sea este código.