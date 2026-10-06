"""Router público para verificación de correo (código OTP de 6 dígitos).

Endpoints:
- POST /email-verification/send   → envía el código (rate limited, devuelve debug_code en dev/e2e)
- POST /email-verification/verify → verifica el código y emite token de verificación
"""

import logging

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.errors import ServiceUnavailableError
from src.core.ratelimit import limiter
from src.db.session import get_db
from src.schemas.email_verification import (
    EmailVerificationSendRequest,
    EmailVerificationSendResponse,
    EmailVerificationVerifyRequest,
    EmailVerificationVerifyResponse,
)
from src.services import email_verification, registration_mail

logger = logging.getLogger("mpv.api")

router = APIRouter(prefix="/email-verification", tags=["email-verification"])


@router.post(
    "/send",
    response_model=EmailVerificationSendResponse,
    status_code=status.HTTP_200_OK,
    summary="Enviar código de verificación de 6 dígitos al correo",
    responses={
        422: {"description": "Email o purpose inválido."},
        429: {"description": "Cooldown activo (60s) o tope horario (5/hora) superado."},
        503: {"description": "No se pudo enviar el correo (Mailtrap sin token o caído)."},
    },
)
@limiter.limit(settings.EMAIL_VERIFICATION_SEND_RATE_LIMIT)
async def send_verification_code(
    request: Request,
    payload: EmailVerificationSendRequest,
    db: AsyncSession = Depends(get_db),
) -> EmailVerificationSendResponse:
    """Genera y envía un código de 6 dígitos al correo indicado.

    Aplica cooldown de 60s entre envíos y tope de 5 envíos por hora móvil.
    Devuelve el código en `debug_code` SOLO si `EMAIL_VERIFICATION_DEBUG_CODE`
    está activo y NO estamos en producción (para tests e2e con Playwright).
    """
    code = await email_verification.send_code(db, email=payload.email, purpose=payload.purpose)

    ok = await registration_mail.send_email_verification_email(
        payload.email, code, settings.EMAIL_VERIFICATION_CODE_TTL_MINUTES
    )

    debug_code = None
    if settings.EMAIL_VERIFICATION_DEBUG_CODE and settings.ENVIRONMENT != "production":
        debug_code = code

    if not ok and debug_code is None:
        raise ServiceUnavailableError(
            "No pudimos enviar el correo de verificación. Intenta de nuevo en unos minutos."
        )
    if not ok:
        # Modo dev/e2e: sin Mailtrap configurado el flujo local no se bloquea; el código viaja
        # en la respuesta y NUNCA se loguea. En producción esta rama no existe (la guarda de
        # arranque aborta con el flag activo).
        logger.warning(
            "Verificación por correo: envío falló en modo debug; se devuelve debug_code."
        )

    return EmailVerificationSendResponse(
        sent=True,
        expires_minutes=settings.EMAIL_VERIFICATION_CODE_TTL_MINUTES,
        resend_seconds=settings.EMAIL_VERIFICATION_RESEND_SECONDS,
        debug_code=debug_code,
    )


@router.post(
    "/verify",
    response_model=EmailVerificationVerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Verificar código de 6 dígitos y obtener token de verificación",
    responses={
        422: {"description": "Código inválido, expirado, ya consumido o formato incorrecto."},
        429: {"description": "Demasiados intentos fallidos (código inutilizado)."},
    },
)
@limiter.limit(settings.EMAIL_VERIFICATION_VERIFY_RATE_LIMIT)
async def verify_verification_code(
    request: Request,
    payload: EmailVerificationVerifyRequest,
    db: AsyncSession = Depends(get_db),
) -> EmailVerificationVerifyResponse:
    """Verifica el código y devuelve el token de verificación (JWT, 15 min).

    El token acredita que ESE correo ha sido verificado para ESE propósito
    (patient/doctor). Los endpoints de alta (`POST /doctors`, `POST /patients`)
    exigen este token cuando `EMAIL_VERIFICATION_REQUIRED` y el payload trae email.
    """
    token = await email_verification.verify_code(
        db, email=payload.email, purpose=payload.purpose, code=payload.code
    )
    return EmailVerificationVerifyResponse(
        verification_token=token,
        expires_minutes=settings.EMAIL_VERIFICATION_TOKEN_TTL_MINUTES,
    )
