"""Token de verificación de correo (emitido tras validar el código OTP de 6 dígitos).

Este token acredita que el correo ha sido verificado para un propósito concreto
(`patient` | `doctor`). Se firma con `EMAIL_VERIFICATION_SECRET` (HS256) y tiene
un TTL corto (`EMAIL_VERIFICATION_TOKEN_TTL_MINUTES`, por defecto 15 min).

El token liga el correo (normalizado) y el propósito: `is_valid_for` exige que
ambos coincidan, así un token de "patient" no sirve para "doctor" ni viceversa,
ni para otro correo.
"""

from datetime import UTC, datetime, timedelta

import jwt

from src.core.config import settings

# Distingue estos tokens de cualquier otro JWT del sistema. Se verifica explícitamente:
# sin esto, un token de consulta o de Supabase con el mismo secreto pasaría por token
# de verificación de correo.
_TOKEN_TYPE = "email_verification"
_ALGORITHM = "HS256"


def issue(email: str, purpose: str) -> str:
    """Emite el token de verificación de correo.

    Args:
        email: Correo normalizado (minúsculas, sin espacios).
        purpose: 'patient' o 'doctor'.

    Returns:
        JWT firmado con `sub` = email, `purpose`, `typ` = 'email_verification',
        `iat` y `exp` (TTL en minutos desde settings).
    """
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": email.lower().strip(),
            "purpose": purpose,
            "typ": _TOKEN_TYPE,
            "iat": now,
            "exp": now + timedelta(minutes=settings.EMAIL_VERIFICATION_TOKEN_TTL_MINUTES),
        },
        settings.EMAIL_VERIFICATION_SECRET,
        algorithm=_ALGORITHM,
    )


def is_valid_for(token: str | None, email: str, purpose: str) -> bool:
    """True si `token` es un token de verificación vigente para ESE correo y propósito.

    Valida firma, `typ`, `sub` (email normalizado) y `purpose`. Sin la comprobación
    de `sub` y `purpose`, un token válido de un correo serviría para otro (IDOR
    con credencial legítima) o un token de "patient" serviría para "doctor".
    """
    if not token:
        return False
    try:
        payload = jwt.decode(token, settings.EMAIL_VERIFICATION_SECRET, algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return False
    return (
        payload.get("typ") == _TOKEN_TYPE
        and payload.get("sub") == email.lower().strip()
        and payload.get("purpose") == purpose
    )
