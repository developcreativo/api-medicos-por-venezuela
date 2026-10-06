# Plan — verificación de correo en el registro

Contrato completo en [spec.md](spec.md).

## Backend

1. Migración `email_verification_codes` (una fila por email+purpose, RLS on sin policies).
2. `src/models/email_verification_code.py`.
3. `src/core/config.py`: secretos, TTLs, intentos, cooldowns, rate limits y flags
   (`EMAIL_VERIFICATION_REQUIRED`, `EMAIL_VERIFICATION_DEBUG_CODE`); docs en `.env.example` y
   `.env.production.example`.
4. `src/main.py`: abortar en producción con `EMAIL_VERIFICATION_DEBUG_CODE=true` y con el secreto
   default.
5. `src/core/email_verification_token.py` (patrón `consultation_token.py`).
6. `src/services/email_verification.py`: `send_code`, `verify_code`, `ensure_verified`, hashing HMAC.
7. `src/core/errors.py` (+ `exceptions.py` si hace falta): `TooManyRequestsError` (429) y
   `ServiceUnavailableError` (503).
8. `registration_mail.py`: `build_email_verification_mail(code, ttl)` con el código grande.
9. `src/schemas/email_verification.py` + router `src/routers/email_verification.py`
   (`send`, `verify`, `debug_code` solo dev).
10. `DoctorCreate` y `PatientCreate`: `email_verification_token` opcional; enforcement en
    `POST /doctors` y `POST /patients` (con `EMAIL_VERIFICATION_REQUIRED`).
11. `tests/conftest.py`: apagar `EMAIL_VERIFICATION_REQUIRED` (como el limiter).
12. `tests/test_email_verification.py`: envío, cooldown, tope horario, verificación, expiración,
    intentos, token (email/purpose), enforcement en altas, 503 sin correo, `debug_code`.
13. `ruff` + suite completa.

## Frontend

14. `lib/emailVerification.ts` + campo token en `lib/doctors.ts` / `lib/patients.ts`.
15. `components/ConfirmarCorreoModal.tsx` (correo grande/negritas) y
    `components/VerificacionCodigoModal.tsx` (6 dígitos, reenviar con countdown, nota de spam).
16. `pages/registro-paciente.tsx`: quitar el checkbox "Conozco la especialidad" (select siempre
    visible, opcional), encadenar confirmación → código → alta con token; `authedPatient` se salta.
17. `pages/registro-medico.tsx`: confirmación → código → alta con token; `incomplete` se salta.
18. E2E: helper de verificación (captura `debug_code` de la respuesta) en `registro-paciente` y
    `mi-caso-videoconsulta`; quitar el checkbox del spec; assert de "Otra" sobre el select visible.
19. `tsc`, `lint`, `build` y `pnpm test:e2e` verdes; `changeslog.md`.

## Operación

20. `.env.production`: `EMAIL_VERIFICATION_SECRET`, Mailtrap (`MAILTRAP_API_TOKEN`,
    `MAIL_FROM_*`), `EMAIL_VERIFICATION_DEBUG_CODE=false`; migración; revisar la confirmación de
    correo de Supabase.