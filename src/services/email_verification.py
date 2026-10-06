"""Servicio de verificación de correo con código OTP de 6 dígitos.

Flujo:
1. `send_code`: genera un código de 6 dígitos, lo hashea con HMAC-SHA256, hace upsert
   en `email_verification_codes` (una fila por email+purpose), aplica cooldown y tope
   horario, y devuelve el código EN CLARO para que el router lo envíe por correo.
   NUNCA se loguea el código.
2. `verify_code`: valida el código comparando hashes con `hmac.compare_digest`,
   gestiona intentos y expiración, y si es correcto emite un token de verificación
   (`email_verification_token.issue`).
3. `ensure_verified`: gate que los endpoints de alta llaman antes de crear; exige
   token válido cuando `EMAIL_VERIFICATION_REQUIRED` y hay correo en el payload.
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.email_verification_token import (
    is_valid_for as is_valid_verification_token,
)
from src.core.email_verification_token import (
    issue as issue_verification_token,
)
from src.core.errors import (
    ForbiddenError,
    TooManyRequestsError,
    UnprocessableError,
)
from src.models.email_verification_code import EmailVerificationCode


def normalize_email(email: str) -> str:
    """Normaliza el correo a minúsculas y sin espacios."""
    return email.lower().strip()


def hash_code(email: str, purpose: str, code: str) -> str:
    """HMAC-SHA256 del código ligado a email y purpose."""
    message = f"{email}:{purpose}:{code}".encode()
    return hmac.new(
        settings.EMAIL_VERIFICATION_SECRET.encode(), message, hashlib.sha256
    ).hexdigest()


async def send_code(session: AsyncSession, *, email: str, purpose: str) -> str:
    """Genera y almacena un nuevo código OTP, y devuelve el código en claro.

    El router usa este código para enviarlo por correo. NUNCA se loguea.

    Raises:
        TooManyRequestsError: si no pasó el cooldown (60s) o se superó el tope
            horario (5 envíos/hora).
    """
    email_norm = normalize_email(email)
    now = datetime.now(UTC)

    # 1) Buscar fila existente
    stmt = select(EmailVerificationCode).where(
        EmailVerificationCode.email == email_norm,
        EmailVerificationCode.purpose == purpose,
    )
    row = await session.scalar(stmt)

    # 2) Cooldown: si existe y last_sent_at es reciente, 429 sin escribir
    if row is not None:
        elapsed = (now - row.last_sent_at).total_seconds()
        if elapsed < settings.EMAIL_VERIFICATION_RESEND_SECONDS:
            raise TooManyRequestsError("Espera unos segundos antes de pedir otro código.")

    # 3) Ventana horaria: reiniciar o incrementar contador
    if row is not None:
        window_elapsed = (now - row.window_started_at).total_seconds()
        if window_elapsed >= 3600:  # 1 hora
            row.window_started_at = now
            row.sends_count = 0
        elif row.sends_count >= settings.EMAIL_VERIFICATION_MAX_SENDS_PER_HOUR:
            raise TooManyRequestsError(
                "Ya pediste varios códigos. Espera una hora o contacta a soporte."
            )
        row.sends_count += 1
    else:
        row = EmailVerificationCode(
            email=email_norm,
            purpose=purpose,
            window_started_at=now,
            sends_count=1,
        )
        session.add(row)

    # 4) Generar código y actualizar fila
    code = f"{secrets.randbelow(1_000_000):06d}"
    row.code_hash = hash_code(email_norm, purpose, code)
    row.expires_at = now + timedelta(minutes=settings.EMAIL_VERIFICATION_CODE_TTL_MINUTES)
    row.attempts = 0
    row.consumed_at = None
    row.last_sent_at = now

    await session.commit()
    return code


async def verify_code(session: AsyncSession, *, email: str, purpose: str, code: str) -> str:
    """Verifica el código y, si es correcto, emite y devuelve el token de verificación.

    Raises:
        UnprocessableError: si no hay fila, expiró, ya se consumió, o el código
            es incorrecto (sin superar MAX_ATTEMPTS).
        TooManyRequestsError: si se superó MAX_ATTEMPTS (marca consumido).
    """
    email_norm = normalize_email(email)
    now = datetime.now(UTC)

    stmt = select(EmailVerificationCode).where(
        EmailVerificationCode.email == email_norm,
        EmailVerificationCode.purpose == purpose,
    )
    row = await session.scalar(stmt)

    # Sin fila, consumida o expirada
    if row is None or row.consumed_at is not None or row.expires_at <= now:
        raise UnprocessableError("El código expiró o no existe. Pide uno nuevo.")

    # Comparar hash (timing-safe)
    expected_hash = hash_code(email_norm, purpose, code)
    if not hmac.compare_digest(expected_hash, row.code_hash):
        row.attempts += 1
        await session.commit()
        if row.attempts >= settings.EMAIL_VERIFICATION_MAX_ATTEMPTS:
            row.consumed_at = now
            await session.commit()
            raise TooManyRequestsError("Demasiados intentos. Pide un código nuevo.")
        raise UnprocessableError("Código incorrecto.")

    # Correcto: marcar consumido y emitir token
    row.consumed_at = now
    await session.commit()
    return issue_verification_token(email_norm, purpose)


def ensure_verified(*, token: str | None, email: str | None, purpose: str) -> None:
    """Gate de verificación para los endpoints de alta.

    Si `EMAIL_VERIFICATION_REQUIRED` y hay correo en el payload, exige token
    válido para ese correo y propósito. Sin correo (alta anónima de paciente),
    no se exige nada.

    Raises:
        ForbiddenError: si se requiere verificación y el token falta o es inválido.
    """
    if not settings.EMAIL_VERIFICATION_REQUIRED:
        return
    if email is None:
        return
    if not token or not is_valid_verification_token(token, email, purpose):
        raise ForbiddenError("Verifica tu correo antes de continuar.")
