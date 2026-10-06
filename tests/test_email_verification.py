"""Pruebas de integración de la verificación de correo (código OTP de 6 dígitos).

8 casos cubiertos (según spec):
1. Envío + código (send_code devuelve código, upsert correcto).
2. Cooldown (60s) y tope horario (5/hora) → 429.
3. Intentos: 5 fallos marcan consumido y 429; 4 fallos + 1 éxito → token.
4. Token: liga email + purpose; token de patient no sirve para doctor y viceversa.
5. Expiración: código de 60 min y token de 15 min.
6. Enforcement: POST /doctors y POST /patients exigen token cuando
   EMAIL_VERIFICATION_REQUIRED + hay email; sin email (paciente anónimo) no exige.
7. 503 si Mailtrap sin token o caído.
8. debug_code solo con EMAIL_VERIFICATION_DEBUG_CODE y ENVIRONMENT != production.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.email_verification_token import is_valid_for, issue
from src.models.email_verification_code import EmailVerificationCode
from src.services import email_verification

PREFIX = "/api/v1"


# --- Helpers ------------------------------------------------------------------


async def _get_row(
    db_session: AsyncSession, email: str, purpose: str
) -> EmailVerificationCode | None:
    stmt = select(EmailVerificationCode).where(
        EmailVerificationCode.email == email.lower().strip(),
        EmailVerificationCode.purpose == purpose,
    )
    return await db_session.scalar(stmt)


async def _send_code(db_session: AsyncSession, email: str, purpose: str) -> str:
    """Llama al servicio y devuelve el código (simula lo que hace el router)."""
    return await email_verification.send_code(db_session, email=email, purpose=purpose)


async def _send_code_bypass_cooldown(db_session: AsyncSession, email: str, purpose: str) -> str:
    """Envía código manipulando last_sent_at para evitar cooldown en tests."""
    code = await _send_code(db_session, email, purpose)
    # Manipula last_sent_at para que parezca que pasó mucho tiempo
    row = await _get_row(db_session, email, purpose)
    row.last_sent_at = datetime.now(UTC) - timedelta(minutes=10)
    await db_session.commit()
    return code


# --- 1. Envío + código ---------------------------------------------------------


async def test_send_code_creates_row_and_returns_code(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """send_code hace upsert, fija hash/expiración/contadores y devuelve el código."""
    code = await _send_code(db_session, "test@ejemplo.com", "patient")

    assert len(code) == 6 and code.isdigit()

    row = await _get_row(db_session, "test@ejemplo.com", "patient")
    assert row is not None
    assert row.email == "test@ejemplo.com"
    assert row.purpose == "patient"
    assert row.code_hash  # hash presente
    assert row.attempts == 0
    assert row.sends_count == 1
    assert row.expires_at > datetime.now(UTC)
    assert row.consumed_at is None


async def test_send_code_upsert_replaces_existing_code(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Segundo envío al mismo email+purpose reemplaza hash y resetea attempts."""
    await _send_code_bypass_cooldown(db_session, "upsert@ejemplo.com", "doctor")

    await _send_code_bypass_cooldown(db_session, "upsert@ejemplo.com", "doctor")

    row = await _get_row(db_session, "upsert@ejemplo.com", "doctor")
    assert row.sends_count == 2
    assert row.attempts == 0  # reseteado


# --- 2. Cooldown y tope horario ------------------------------------------------


async def test_cooldown_60s_raises_429(anon_client: AsyncClient, db_session: AsyncSession) -> None:
    """Reenviar antes de 60s lanza TooManyRequestsError (429)."""
    await _send_code(db_session, "cooldown@ejemplo.com", "patient")

    with pytest.raises(email_verification.TooManyRequestsError):
        await _send_code(db_session, "cooldown@ejemplo.com", "patient")


async def test_hourly_limit_5_per_hour_raises_429(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """5 envíos en la misma ventana horaria; el 6º lanza 429."""
    email = "limit@ejemplo.com"
    for _ in range(5):
        await _send_code_bypass_cooldown(db_session, email, "patient")

    with pytest.raises(email_verification.TooManyRequestsError):
        await _send_code_bypass_cooldown(db_session, email, "patient")


async def test_hourly_window_resets_after_1_hour(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Pasada 1 hora, la ventana se reinicia y permite 5 más."""
    email = "window@ejemplo.com"
    for _ in range(5):
        await _send_code_bypass_cooldown(db_session, email, "patient")

    # Simula que pasó 1 hora + 1 seg
    row = await _get_row(db_session, email, "patient")
    row.window_started_at = datetime.now(UTC) - timedelta(hours=1, seconds=1)
    await db_session.commit()

    # Ahora debe permitir (ventana reiniciada)
    code = await _send_code_bypass_cooldown(db_session, email, "patient")
    assert code is not None

    row = await _get_row(db_session, email, "patient")
    assert row.sends_count == 1  # contador reiniciado


# --- 3. Intentos ---------------------------------------------------------------


async def test_five_failed_attempts_marks_consumed_and_429(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """5 intentos fallidos → fila marcada consumed_at + TooManyRequestsError."""
    email = "attempts@ejemplo.com"
    await _send_code(db_session, email, "patient")

    # 4 intentos fallidos
    for _ in range(4):
        with pytest.raises(email_verification.UnprocessableError):
            await email_verification.verify_code(
                db_session, email=email, purpose="patient", code="000000"
            )

    # 5º intento: marca consumido y lanza 429
    with pytest.raises(email_verification.TooManyRequestsError):
        await email_verification.verify_code(
            db_session, email=email, purpose="patient", code="000000"
        )

    row = await _get_row(db_session, email, "patient")
    assert row.consumed_at is not None
    assert row.attempts == 5


async def test_four_fail_then_success_returns_token(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """4 fallos y luego éxito emite token y marca consumed."""
    email = "success@ejemplo.com"
    code = await _send_code(db_session, email, "patient")

    # 4 fallos
    for _ in range(4):
        with pytest.raises(email_verification.UnprocessableError):
            await email_verification.verify_code(
                db_session, email=email, purpose="patient", code="000000"
            )

    # Éxito con el código real
    token = await email_verification.verify_code(
        db_session, email=email, purpose="patient", code=code
    )

    assert token is not None
    assert isinstance(token, str)
    assert len(token) > 0

    # Token válido para ese email/purpose
    assert is_valid_for(token, email, "patient")

    row = await _get_row(db_session, email, "patient")
    assert row.consumed_at is not None


async def test_wrong_code_increments_attempts(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Código incorrecto incrementa attempts y devuelve UnprocessableError."""
    email = "wrong@ejemplo.com"
    await _send_code(db_session, email, "patient")

    with pytest.raises(email_verification.UnprocessableError) as exc:
        await email_verification.verify_code(
            db_session, email=email, purpose="patient", code="111111"
        )
    assert "Código incorrecto" in str(exc.value)

    row = await _get_row(db_session, email, "patient")
    assert row.attempts == 1


# --- 4. Token liga email + purpose ---------------------------------------------


async def test_token_binds_email_and_purpose() -> None:
    """Token emitido para patient/email no sirve para doctor/otro-email."""
    token_patient = issue("user@ejemplo.com", "patient")
    token_doctor = issue("user@ejemplo.com", "doctor")

    # Mismo email, purpose distinto → False
    assert is_valid_for(token_patient, "user@ejemplo.com", "patient")
    assert not is_valid_for(token_patient, "user@ejemplo.com", "doctor")
    assert not is_valid_for(token_doctor, "user@ejemplo.com", "patient")
    assert is_valid_for(token_doctor, "user@ejemplo.com", "doctor")

    # Distinto email, mismo purpose → False
    assert not is_valid_for(token_patient, "otro@ejemplo.com", "patient")


async def test_verify_endpoint_returns_token_bound_to_email_purpose(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """POST /verify devuelve token que solo valida para ese email/purpose."""
    # Set MAILTRAP_API_TOKEN for this test
    original_token = settings.MAILTRAP_API_TOKEN
    settings.MAILTRAP_API_TOKEN = "test-token"
    try:
        email = "verify@ejemplo.com"
        code = await _send_code_bypass_cooldown(db_session, email, "patient")

        resp = await anon_client.post(
            f"{PREFIX}/email-verification/verify",
            json={"email": email, "purpose": "patient", "code": code},
        )
        assert resp.status_code == 200
        token = resp.json()["verification_token"]

        assert is_valid_for(token, email, "patient")
        assert not is_valid_for(token, email, "doctor")
        assert not is_valid_for(token, "otro@ejemplo.com", "patient")
    finally:
        settings.MAILTRAP_API_TOKEN = original_token


# --- 5. Expiración -------------------------------------------------------------


async def test_code_expires_after_ttl(anon_client: AsyncClient, db_session: AsyncSession) -> None:
    """Código expira a los EMAIL_VERIFICATION_CODE_TTL_MINUTES (60 min)."""
    email = "expire@ejemplo.com"
    await _send_code(db_session, email, "patient")

    # Avanza el tiempo 61 minutos
    row = await _get_row(db_session, email, "patient")
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    with pytest.raises(email_verification.UnprocessableError) as exc:
        await email_verification.verify_code(
            db_session, email=email, purpose="patient", code="000000"
        )
    assert "expiró" in str(exc.value).lower()


async def test_token_expires_after_token_ttl() -> None:
    """Token de verificación expira a los EMAIL_VERIFICATION_TOKEN_TTL_MINUTES (15 min)."""
    token = issue("tokenexp@ejemplo.com", "patient")

    # Avanza 16 min manipulando el exp del payload (simulación directa)
    import jwt

    payload = jwt.decode(
        token,
        settings.EMAIL_VERIFICATION_SECRET,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    payload["exp"] = int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())
    expired_token = jwt.encode(payload, settings.EMAIL_VERIFICATION_SECRET, algorithm="HS256")

    assert not is_valid_for(expired_token, "tokenexp@ejemplo.com", "patient")


# --- 6. Enforcement en POST /doctors y POST /patients --------------------------


async def test_doctor_register_requires_token_when_required(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """POST /doctors exige email_verification_token si flag activo y hay email."""
    settings.EMAIL_VERIFICATION_REQUIRED = True
    settings.MAILTRAP_API_TOKEN = "test-token"
    try:
        # Primero creamos el professional_type_id necesario
        from src.models.professional_type import ProfessionalType

        pt = await db_session.scalar(
            select(ProfessionalType).where(ProfessionalType.name == "Médico")
        )
        assert pt is not None

        # Intento sin token → 403
        payload = {
            "professional_type_id": str(pt.id),
            "cedula": "V-12345678",
            "full_name": "Dr Test",
            "phone": "+5804145200715",
            "email": "doctor@test.com",
        }
        resp = await anon_client.post(f"{PREFIX}/doctors", json=payload)
        assert resp.status_code == 403
        assert "Verifica tu correo" in resp.json()["detail"]

        # Con token válido → 201 (mockeamos SACS/FPV)
        from src.schemas.sacs import SacsVerificationResponse

        with patch(
            "src.services.sacs.verificar_sacs",
            AsyncMock(
                return_value=SacsVerificationResponse(
                    encontrado=True,
                    es_medico=True,
                    nombre="JUAN",
                    apellido="PEREZ",
                    licencia="MPPS-11111",
                )
            ),
        ):
            token = issue("doctor@test.com", "doctor")
            payload["email_verification_token"] = token
            resp = await anon_client.post(f"{PREFIX}/doctors", json=payload)
            # Puede fallar por SACS mock, pero no por 403 de verificación
            assert resp.status_code != 403
    finally:
        settings.EMAIL_VERIFICATION_REQUIRED = False
        settings.MAILTRAP_API_TOKEN = ""


async def test_patient_register_requires_token_when_required_and_has_email(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """POST /patients exige token si flag activo y hay email; sin email no exige."""
    settings.EMAIL_VERIFICATION_REQUIRED = True
    settings.MAILTRAP_API_TOKEN = "test-token"
    try:
        # Con email y SIN token → 403
        payload = {
            "full_name": "Paciente Test",
            "phone_whatsapp": "+58412000000",
            "affected_zone": "Caracas",
            "emergency_phone": "+58412000001",
            "email": "paciente@test.com",
        }
        resp = await anon_client.post(f"{PREFIX}/patients", json=payload)
        assert resp.status_code == 403

        # Con email Y token válido → 201
        token = issue("paciente@test.com", "patient")
        payload["email_verification_token"] = token
        resp = await anon_client.post(f"{PREFIX}/patients", json=payload)
        assert resp.status_code == 201

        # SIN email (alta anónima) → 201 sin token
        payload_no_email = {
            "full_name": "Anónimo",
            "phone_whatsapp": "+58412000002",
            "affected_zone": "Maracaibo",
            "emergency_phone": "+58412000003",
            "email": None,
            "email_verification_token": None,
        }
        resp = await anon_client.post(f"{PREFIX}/patients", json=payload_no_email)
        assert resp.status_code == 201
    finally:
        settings.EMAIL_VERIFICATION_REQUIRED = False
        settings.MAILTRAP_API_TOKEN = ""


# --- 7. 503 si Mailtrap sin token / caído --------------------------------------
#
# ⚠️ Los dos fijan `EMAIL_VERIFICATION_DEBUG_CODE = False` y no lo heredan del `.env`: con el flag
# encendido el endpoint NO devuelve 503, sino 200 con `debug_code` (es justo lo que ese modo
# existe para hacer, ver sección 8). Sin fijarlo, un `.env` de desarrollo con el flag en `true`
# —el de quien esté grabando una demo del registro, por ejemplo— tumba estos tests sin que el
# código tenga nada que ver. Cada test monta el entorno que necesita para lo que afirma.


async def test_send_returns_503_when_mail_disabled(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Si Mailtrap no está configurado (sin token), el endpoint devuelve 503."""
    # Asegura que no hay token
    original_token = settings.MAILTRAP_API_TOKEN
    original_debug = settings.EMAIL_VERIFICATION_DEBUG_CODE
    settings.MAILTRAP_API_TOKEN = ""
    settings.EMAIL_VERIFICATION_DEBUG_CODE = False
    try:
        resp = await anon_client.post(
            f"{PREFIX}/email-verification/send",
            json={"email": "nomail@ejemplo.com", "purpose": "patient"},
        )
        assert resp.status_code == 503
        assert "no disponible" in resp.json()["detail"].lower()
    finally:
        settings.MAILTRAP_API_TOKEN = original_token
        settings.EMAIL_VERIFICATION_DEBUG_CODE = original_debug


async def test_send_returns_503_when_mailtrap_fails(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Si Mailtrap falla (excepción), el endpoint devuelve 503."""
    original_token = settings.MAILTRAP_API_TOKEN
    original_debug = settings.EMAIL_VERIFICATION_DEBUG_CODE
    settings.MAILTRAP_API_TOKEN = "fake-token"
    settings.EMAIL_VERIFICATION_DEBUG_CODE = False
    try:
        with patch(
            "src.services.registration_mail.send_mail",
            AsyncMock(side_effect=Exception("Mailtrap down")),
        ):
            resp = await anon_client.post(
                f"{PREFIX}/email-verification/send",
                json={"email": "fail@ejemplo.com", "purpose": "patient"},
            )
            assert resp.status_code == 503
    finally:
        settings.MAILTRAP_API_TOKEN = original_token
        settings.EMAIL_VERIFICATION_DEBUG_CODE = original_debug


# --- 8. debug_code solo en dev/e2e ---------------------------------------------


async def test_debug_code_returned_when_flag_on_and_not_production(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """debug_code en respuesta SOLO si DEBUG_CODE=True y ENV!=production."""
    original_debug = settings.EMAIL_VERIFICATION_DEBUG_CODE
    original_env = settings.ENVIRONMENT
    original_token = settings.MAILTRAP_API_TOKEN
    settings.EMAIL_VERIFICATION_DEBUG_CODE = True
    settings.ENVIRONMENT = "development"
    settings.MAILTRAP_API_TOKEN = "test-token"
    try:
        with patch("src.services.registration_mail.send_mail", AsyncMock(return_value=True)):
            resp = await anon_client.post(
                f"{PREFIX}/email-verification/send",
                json={"email": "debug@ejemplo.com", "purpose": "patient"},
            )
            assert resp.status_code == 200
            assert "debug_code" in resp.json()
            assert resp.json()["debug_code"] is not None
            assert len(resp.json()["debug_code"]) == 6
    finally:
        settings.EMAIL_VERIFICATION_DEBUG_CODE = original_debug
        settings.ENVIRONMENT = original_env
        settings.MAILTRAP_API_TOKEN = original_token


async def test_debug_code_not_returned_when_flag_off(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """debug_code ausente si DEBUG_CODE=False (aunque ENV=development)."""
    original_debug = settings.EMAIL_VERIFICATION_DEBUG_CODE
    original_env = settings.ENVIRONMENT
    original_token = settings.MAILTRAP_API_TOKEN
    settings.EMAIL_VERIFICATION_DEBUG_CODE = False
    settings.ENVIRONMENT = "development"
    settings.MAILTRAP_API_TOKEN = "test-token"
    try:
        with patch("src.services.registration_mail.send_mail", AsyncMock(return_value=True)):
            resp = await anon_client.post(
                f"{PREFIX}/email-verification/send",
                json={"email": "nodebug@ejemplo.com", "purpose": "patient"},
            )
            assert resp.status_code == 200
            assert resp.json().get("debug_code") is None
    finally:
        settings.EMAIL_VERIFICATION_DEBUG_CODE = original_debug
        settings.ENVIRONMENT = original_env
        settings.MAILTRAP_API_TOKEN = original_token


async def test_debug_code_not_returned_in_production_even_if_flag_on(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """En producción, debug_code NUNCA se devuelve (aunque el flag esté on).
    Nota: el arranque ya aborta si ambos están on, pero testeamos la lógica del router.
    """
    original_debug = settings.EMAIL_VERIFICATION_DEBUG_CODE
    original_env = settings.ENVIRONMENT
    original_token = settings.MAILTRAP_API_TOKEN
    settings.EMAIL_VERIFICATION_DEBUG_CODE = True
    settings.ENVIRONMENT = "production"
    settings.MAILTRAP_API_TOKEN = "test-token"
    try:
        with patch("src.services.registration_mail.send_mail", AsyncMock(return_value=True)):
            resp = await anon_client.post(
                f"{PREFIX}/email-verification/send",
                json={"email": "proddbg@ejemplo.com", "purpose": "patient"},
            )
            assert resp.status_code == 200
            assert resp.json().get("debug_code") is None
    finally:
        settings.EMAIL_VERIFICATION_DEBUG_CODE = original_debug
        settings.ENVIRONMENT = original_env
        settings.MAILTRAP_API_TOKEN = original_token


async def test_debug_code_se_devuelve_aunque_falle_el_correo(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """En dev/e2e el flujo no se bloquea si Mailtrap no está configurado: el código viaja en la
    respuesta. En producción esta rama no existe (el arranque aborta con el flag)."""
    original_debug = settings.EMAIL_VERIFICATION_DEBUG_CODE
    original_env = settings.ENVIRONMENT
    original_token = settings.MAILTRAP_API_TOKEN
    settings.EMAIL_VERIFICATION_DEBUG_CODE = True
    settings.ENVIRONMENT = "development"
    settings.MAILTRAP_API_TOKEN = ""
    try:
        resp = await anon_client.post(
            f"{PREFIX}/email-verification/send",
            json={"email": "debugfallback@ejemplo.com", "purpose": "patient"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["debug_code"] is not None
    finally:
        settings.EMAIL_VERIFICATION_DEBUG_CODE = original_debug
        settings.ENVIRONMENT = original_env
        settings.MAILTRAP_API_TOKEN = original_token
