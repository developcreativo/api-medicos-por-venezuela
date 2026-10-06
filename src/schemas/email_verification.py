"""Esquemas Pydantic para la verificación de correo (código OTP de 6 dígitos)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class EmailVerificationSendRequest(BaseModel):
    """Petición para enviar un código de verificación."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    purpose: Literal["patient", "doctor"]


class EmailVerificationSendResponse(BaseModel):
    """Respuesta del envío del código."""

    sent: bool
    expires_minutes: int
    resend_seconds: int
    debug_code: str | None = None


class EmailVerificationVerifyRequest(BaseModel):
    """Petición para verificar el código."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    purpose: Literal["patient", "doctor"]
    code: str = Field(..., pattern=r"^\d{6}$")


class EmailVerificationVerifyResponse(BaseModel):
    """Respuesta de la verificación exitosa."""

    verification_token: str
    expires_minutes: int
