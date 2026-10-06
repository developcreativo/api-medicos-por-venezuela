"""Esquemas Pydantic para patients (Create / Update / Response)."""

import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from src.schemas.clinical import ClinicalAccessMixin, ClinicalSummary


class PatientBase(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=200)
    phone_whatsapp: str = Field(..., min_length=5, max_length=30)
    affected_zone: str = Field(..., min_length=2, max_length=100)
    cedula: str | None = Field(default=None, max_length=20)
    age_range: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    needs_tags: list[Annotated[str, Field(max_length=100)]] = Field(
        default_factory=list, max_length=20
    )
    description: str | None = Field(default=None, max_length=2000)
    user_id: uuid.UUID | None = None
    allergies: str | None = Field(default=None, max_length=500)
    # Carga familiar: si parent_id viene, este registro es un menor a cargo de otro
    # patient (el adulto responsable); parentesco describe esa relación.
    parent_id: uuid.UUID | None = None
    parentesco: str | None = Field(default=None, max_length=50)


class PatientCreate(PatientBase):
    model_config = ConfigDict(extra="forbid")

    # El insert exige consentimiento (ver política RLS patients_insert_public).
    consent: bool = True

    # Teléfono de emergencia obligatorio en el alta pública.
    emergency_phone: str = Field(..., min_length=5, max_length=30)
    # Dirección cifrada E2E (v1:base64 sealed box). LEGADO: el alta pública ya no la pide
    # (2026-09-27); se sigue aceptando, cifrada, por compatibilidad con clientes viejos.
    address_encrypted: str | None = Field(
        default=None, pattern=r"^v1:[A-Za-z0-9+/=]+$", max_length=4000
    )
    # Token de verificación de correo (emitido por POST /email-verification/verify).
    # Obligatorio cuando EMAIL_VERIFICATION_REQUIRED=true y el payload trae email.
    # En pacientes, el email es opcional (alta anónima por API sin correo): si no hay
    # email, no se exige el token.
    email_verification_token: str | None = None

    @model_validator(mode="after")
    def _validaciones_alta_publica(self) -> "PatientCreate":
        # parent_id y parentesco van juntos (heredado de PatientBase).
        if (self.parent_id is None) != (self.parentesco is None):
            raise ValueError("parent_id y parentesco deben venir juntos, o ninguno de los dos.")

        # Normaliza ambos teléfonos a solo dígitos y exige que sean distintos.
        whatsapp_digits = re.sub(r"\D", "", self.phone_whatsapp)
        emergency_digits = re.sub(r"\D", "", self.emergency_phone)
        if whatsapp_digits == emergency_digits:
            raise ValueError("El teléfono de emergencia debe ser distinto al de WhatsApp.")
        return self


class PatientUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2)
    phone_whatsapp: str | None = Field(default=None, min_length=5)
    affected_zone: str | None = Field(default=None, min_length=2)
    cedula: str | None = None
    age_range: str | None = None
    email: EmailStr | None = None
    needs_tags: list[str] | None = None
    # Clínicos: solo los escribe el médico que trata al paciente (403 al resto, ver el servicio).
    description: str | None = Field(default=None, max_length=2000)
    allergies: str | None = Field(default=None, max_length=500)
    parent_id: uuid.UUID | None = None
    parentesco: str | None = None
    # Opcionales en la actualización (misma validación que el alta si se envían).
    emergency_phone: str | None = Field(default=None, min_length=5, max_length=30)
    address_encrypted: str | None = Field(
        default=None, pattern=r"^v1:[A-Za-z0-9+/=]+$", max_length=4000
    )

    @model_validator(mode="after")
    def _validar_emergencia_distinta_whatsapp(self) -> "PatientUpdate":
        if self.emergency_phone is not None and self.phone_whatsapp is not None:
            whatsapp_digits = re.sub(r"\D", "", self.phone_whatsapp)
            emergency_digits = re.sub(r"\D", "", self.emergency_phone)
            if whatsapp_digits == emergency_digits:
                raise ValueError("El teléfono de emergencia debe ser distinto al de WhatsApp.")
        return self


class DoctorPatientCreate(BaseModel):
    """Alta de un paciente **de consultorio**, hecha por su médico para pedir una interconsulta.

    Formulario deliberadamente corto: solo lo que un especialista necesita para evaluar el caso.
    No pide `phone_whatsapp` ni `affected_zone` (obligatorios en el alta pública) porque este
    paciente no entra a la cola y nadie de la plataforma lo va a contactar — la relación con él
    la mantiene su médico. Pedir esos datos sería guardar PII que no usamos.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(..., min_length=2, max_length=200)
    age_range: str | None = Field(default=None, max_length=20)
    cedula: str | None = Field(default=None, max_length=20)
    allergies: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    # Opcionales acá, a diferencia del alta pública. Si el médico los tiene, se guardan.
    phone_whatsapp: str | None = Field(default=None, min_length=5, max_length=30)
    affected_zone: str | None = Field(default=None, min_length=2, max_length=100)
    emergency_phone: str | None = Field(default=None, min_length=5, max_length=30)
    # Sin default `true` (a diferencia de PatientCreate): acá el médico ATESTIGUA que su paciente
    # autorizó compartir el caso. Una atestación que el cliente puede omitir no es una atestación.
    consent: bool = False


class DoctorPatientUpdate(BaseModel):
    """Edición de un paciente propio. Ni `consent` ni el dueño se tocan por acá."""

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    age_range: str | None = Field(default=None, max_length=20)
    cedula: str | None = Field(default=None, max_length=20)
    allergies: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    phone_whatsapp: str | None = Field(default=None, min_length=5, max_length=30)
    affected_zone: str | None = Field(default=None, min_length=2, max_length=100)
    emergency_phone: str | None = Field(default=None, min_length=5, max_length=30)


class PatientResponse(PatientBase, ClinicalAccessMixin):
    """Ficha del paciente. `description` y `allergies` son clínicos (cifrados en la BD): salen
    en null salvo que el router conceda acceso (el propio paciente o un médico que lo trata).
    El admin gestiona la ficha pero no lee su contenido clínico."""

    model_config = ConfigDict(from_attributes=True)

    description: ClinicalSummary = None
    allergies: ClinicalSummary = None

    # `str` y no `EmailStr` a propósito (mismo criterio que DoctorResponse.email): FastAPI valida
    # también la RESPUESTA, así que una sola fila histórica con un email mal formado hacía fallar
    # el endpoint ENTERO con 500, no solo esa fila. Hay filas así de la época en que el navegador
    # escribía `patients` directo contra Supabase (anon key + RLS), sin validar formato.
    # El formato se sigue exigiendo donde toca: en PatientCreate/PatientUpdate (entrada).
    email: str | None = None

    # Mismo motivo que `email`, por la otra punta: desde el alta por médico estos dos pueden ser
    # NULL en la BD (ver la migración 20260831_170051). Heredados de PatientBase son OBLIGATORIOS,
    # y FastAPI valida también la RESPUESTA: un solo paciente de consultorio haría fallar con 500
    # el endpoint entero, no esa fila. La exigencia se mantiene donde corresponde, en la ENTRADA
    # del alta pública (PatientCreate).
    phone_whatsapp: str | None = None
    affected_zone: str | None = None

    # Teléfono de emergencia (mismo tratamiento que phone_whatsapp: visible para staff con
    # patients.read). address_encrypted NO se agrega aquí: sale solo en GET /patients/{id}/address.
    emergency_phone: str | None = None

    id: uuid.UUID
    consent: bool
    consent_at: datetime | None = None
    created_at: datetime
    created_by_doctor_id: uuid.UUID | None = None


class PatientAddressResponse(BaseModel):
    """Respuesta del endpoint dedicado GET /patients/{id}/address.

    Solo devuelve la ciphertext E2E (o None si el paciente es anterior a esta migración).
    El servidor NUNCA descifra, loguea ni valida el contenido.
    """

    address_encrypted: str | None = None
