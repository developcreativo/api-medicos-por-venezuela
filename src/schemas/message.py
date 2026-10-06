"""Esquemas Pydantic v2 para el módulo de mensajería (hilos, buzón, adjuntos)."""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.config import settings
from src.schemas.clinical import ClinicalAccessMixin, ClinicalSummary

_TAG_RE = re.compile(r"<[^>]+>")


def _sanitize_text(v: str | None) -> str | None:
    if v is None:
        return None
    cleaned = _TAG_RE.sub("", v).strip()
    return cleaned if cleaned else None


class MessageCreate(BaseModel):
    """Payload para enviar un mensaje en el hilo de una consulta.

    Requiere al menos texto (body) o adjuntos (attachment_ids).
    """

    model_config = ConfigDict(extra="forbid")

    # El tope (CA2.3: 0 a 2000 caracteres) sale de `MESSAGING_MAX_BODY_CHARS`, no de un número
    # cableado aquí: así el OpenAPI publica el mismo límite que valida la API y que pinta el
    # `maxLength` del frontend. Se lee al importar, como el resto de los usos de `settings` en
    # módulos: cambiar la env exige reiniciar el proceso, igual que para todo lo demás.
    body: str | None = Field(
        default=None,
        max_length=settings.MESSAGING_MAX_BODY_CHARS,
        description=(
            f"Texto del mensaje, hasta {settings.MESSAGING_MAX_BODY_CHARS} caracteres "
            "(`MESSAGING_MAX_BODY_CHARS`). Las etiquetas HTML se eliminan."
        ),
    )
    attachment_ids: list[uuid.UUID] = Field(default_factory=list)
    client_msg_id: str | None = Field(default=None, max_length=128)

    @field_validator("body", mode="before")
    @classmethod
    def _validate_body(cls, v: str | None) -> str | None:
        return _sanitize_text(v)

    @model_validator(mode="after")
    def _validate_content(self) -> "MessageCreate":
        if not self.body and not self.attachment_ids:
            raise ValueError("El mensaje requiere al menos texto o un archivo adjunto.")
        return self


class AttachmentResponse(ClinicalAccessMixin):
    """Archivo adjunto clínico en el chat.

    `file_name` se descifra solo con grant clínico; sin él, sale null.
    """

    id: uuid.UUID
    message_id: uuid.UUID | None = None
    consultation_id: uuid.UUID
    uploader_role: str
    uploader_user_id: uuid.UUID | None = None
    file_name: ClinicalSummary
    mime_type: str
    file_size_bytes: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MessageResponse(ClinicalAccessMixin):
    """Mensaje del hilo de una consulta.

    `body` se descifra con grant clínico de la consulta; sin grant, sale null.
    """

    id: uuid.UUID
    consultation_id: uuid.UUID
    sender_role: str
    sender_user_id: uuid.UUID | None = None
    direction: str
    channel: str
    kind: str
    call_session_id: uuid.UUID | None = None
    body: ClinicalSummary
    client_msg_id: str | None = None
    sent_at: datetime
    delivered_at: datetime | None = None
    read_at: datetime | None = None
    delivery_status: str
    attachments: list[AttachmentResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class ReadReceiptResponse(BaseModel):
    marked: int


class VideoCallStartResponse(BaseModel):
    """Respuesta de `POST /consultations/{id}/video-call` (R16).

    `room_url` es la sala Jitsi para que el médico la abra en una ventana aparte: viaja en esta
    respuesta autenticada y solo se le devuelve a él. `message_id` es el mensaje de sistema que
    quedó en el hilo para que el paciente se entere; su cuerpo es solo el texto del aviso, sin
    ninguna URL ni token (CA16.6). El acceso lo arma la interfaz con el `consultation_id` y la
    credencial del lector.
    """

    room_url: str
    message_id: uuid.UUID


class InboxThreadResponse(BaseModel):
    """Fila del buzón del médico (con presencia asimétrica del paciente).

    Solo el médico tratante puede ver `patient_online` y `patient_last_seen_at`.
    """

    consultation_id: uuid.UUID
    code: str
    specialty: str | None = None
    patient_name: str
    status: str
    last_message_at: datetime
    last_direction: str
    unread_count: int
    patient_online: bool
    patient_last_seen_at: datetime | None = None
    active_call: uuid.UUID | None = None


class MessagesThreadResponse(ClinicalAccessMixin):
    """Página del hilo de una consulta: los mensajes y el contador de no leídos del llamante.

    Es el cuerpo de `GET /consultations/{id}/messages`. `unread_count` va aquí (CA3.4) y no
    solo en la cabecera `X-Unread-Count`: el navegador no puede leer una cabecera de una
    respuesta cross-origin si no está en `expose_headers`.
    """

    consultation_id: uuid.UUID
    unread_count: int = Field(
        description="Mensajes de la otra dirección sin `read_at` para el llamante (CA3.4)."
    )
    items: list[MessageResponse] = Field(
        default_factory=list, description="Mensajes de la página, ordenados por `sent_at, id`."
    )
