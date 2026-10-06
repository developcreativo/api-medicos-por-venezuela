"""Servicio de mensajería (médico ↔ paciente).

Reglas duras (.claude/rules/mensajeria.md y security.md):
- Un hilo = una consulta (messages.consultation_id).
- Cuerpos cifrados en reposo (EncryptedText).
- Grant de lectura: solo médico tratante/cadena o paciente dueño. El admin NO lee cuerpos — y
  «tratante» significa estar o haber estado ASIGNADO (ver `is_doctor_in_chain`), no haber dejado
  una fila en `consultation_events`.
- Auditoría clínica: toda lectura concedida registra READ_CLINICAL_DATA.
- Presencia asimétrica: solo el médico tratante ve si el paciente está en línea.
- Adjuntos clínicos: PDF e imágenes (JPG, PNG, WEBP). GIF estrictamente prohibido (422).
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core import consultation_token
from src.core.clinical_crypto import reveal
from src.core.config import settings
from src.core.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnprocessableError,
)
from src.core.observability import correlation_id_ctx
from src.core.security import Principal
from src.models.clinical import Message, MessageAttachment
from src.models.consultation import Consultation
from src.models.consultation_event import ConsultationEvent
from src.models.patient import Patient
from src.models.specialty import Specialty
from src.schemas.clinical import ClinicalGrant, summary_grant, treating_grant
from src.schemas.message import InboxThreadResponse, MessageCreate
from src.services import consultations as consultations_service
from src.services import notifications, storage
from src.services.audit import log_action
from src.services.clinical_access import audit_clinical_read, practices_medicine

logger = logging.getLogger("mpv.messaging")

# Presencia del paciente EN MEMORIA DE PROCESO. Correcto para el despliegue actual (un solo
# uvicorn, ver `.claude/rules/commands.md`): con varias réplicas cada una vería solo a los
# pacientes que le tocaron, así que el buzón diría "Desconectado" de alguien que está escribiendo
# contra otra réplica. El respaldo persistente es `consultations.patient_last_seen_at`, que sí es
# compartido; compartir además el registro en vivo (Redis) es v2 y está fuera de alcance.
_ACTIVE_PATIENTS: dict[uuid.UUID, datetime] = {}


def record_patient_presence(consultation_id: uuid.UUID) -> None:
    """Registra actividad reciente del paciente para esta consulta."""
    _ACTIVE_PATIENTS[consultation_id] = datetime.now(UTC)


def is_patient_online(consultation_id: uuid.UUID, last_seen_at: datetime | None = None) -> bool:
    """Indica si el paciente está en línea.

    Umbral: `MESSAGING_PATIENT_PRESENCE_TTL_SECONDS` desde la última señal, sea del registro
    en memoria o de `consultations.patient_last_seen_at`.
    """
    now = datetime.now(UTC)
    ttl = settings.MESSAGING_PATIENT_PRESENCE_TTL_SECONDS
    active_ts = _ACTIVE_PATIENTS.get(consultation_id)
    if active_ts:
        if (now - active_ts).total_seconds() <= ttl:
            return True
        _ACTIVE_PATIENTS.pop(consultation_id, None)

    if last_seen_at:
        if last_seen_at.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=UTC)
        if (now - last_seen_at).total_seconds() <= ttl:
            return True

    return False


# Qué `event_type` significa «fui el médico tratante de este caso» — y por qué solo ese.
#
# Este criterio concedía grant por la MERA EXISTENCIA de una fila en `consultation_events` con
# `created_by = yo`, y eso mezcla dos cosas que no son la misma: «atendí este caso» y «lo toqué
# administrativamente». Un admin que cierra, reasigna o cambia un estado deja su fila
# (`admin_update`, `closed`, `patient_no_show`, `derived`…) y con ella obtenía
# `treating_grant("assigned_doctor")`: leía la conversación descifrada. Va contra `security.md`
# («ser admin nunca concede lectura clínica»), contra `.claude/rules/mensajeria.md` y contra el
# fail-closed de CA3.2 (el admin recibe los cuerpos en `null`). Backlog C-13.
#
# De los `event_type` que escribe el producto, **solo `opened`** implica haber sido el tratante:
# lo escriben `claim_consultation` y `start_scheduled_consultation`, que en el MISMO `UPDATE`
# ponen `assigned_doctor_id` en el actor. Los demás los deja quien no atendió nada: `derived`
# sobre un caso aún sin asignar, `closed`/`patient_no_show` un admin que cierra, `admin_update`
# un admin por definición, y cualquier texto libre vía `POST /consultations/{id}/events`.
#
# Si se añade un evento nuevo que signifique «tomé el caso», va en este conjunto — y solo si su
# escritura garantiza, como las dos de arriba, que el actor queda asignado.
_TREATING_EVENT_TYPES = frozenset({"opened"})


async def is_doctor_in_chain(
    session: AsyncSession, consultation: Consultation, principal: Principal
) -> bool:
    """¿El llamante es, o fue, el médico TRATANTE de este hilo? (CA3.1 y CA1.3)

    Tres señales, todas condicionadas a **ejercer como médico habilitado**:

    1. es el `assigned_doctor_id` actual de la consulta;
    2. lo es de alguna consulta anterior de la cadena — el tramo que sí atendió (CA1.3);
    3. dejó un evento `opened` en ESTA consulta: la tomó y después se la reasignaron, así que su
       asignación ya no está en la fila pero el claim sí quedó escrito.

    La condición de ejercer es la misma de `clinical_access.treating_doctor_grant`
    (`practices_medicine` + asignado), que es la que usa el resto del repo. Tenerla distinta aquí
    era la causa de que la mensajería concediera lo que los demás módulos niegan: una sola
    definición del criterio (misma lección que `doctors._blocked_reason`).

    Un admin que solo gestionó el caso no entra por ninguna de las tres. Qué hacer con el `False`
    lo decide cada llamante: metadatos sin cuerpos en `list_messages` (CA3.2), 403 en
    `mark_as_read` y en la descarga de adjuntos, 404 en las escrituras y en la videollamada.
    """
    # Un admin que no ejerce no es tratante de nada, dé la vuelta que dé por los eventos.
    if not practices_medicine(principal):
        return False

    doctor_user_id = principal.id
    if consultation.assigned_doctor_id == doctor_user_id:
        return True

    # Tomó el caso y luego se lo reasignaron o lo derivó: el claim quedó en el evento.
    stmt = (
        select(ConsultationEvent.id)
        .where(
            ConsultationEvent.consultation_id == consultation.id,
            ConsultationEvent.created_by == doctor_user_id,
            ConsultationEvent.event_type.in_(_TREATING_EVENT_TYPES),
        )
        .limit(1)
    )
    if (await session.scalar(stmt)) is not None:
        return True

    # Recorrer ancestros (parent_consultation_id)
    curr = consultation
    seen = {curr.id}
    while curr.parent_consultation_id is not None and curr.parent_consultation_id not in seen:
        seen.add(curr.parent_consultation_id)
        parent = await session.get(Consultation, curr.parent_consultation_id)
        if parent is None:
            break
        if parent.assigned_doctor_id == doctor_user_id:
            return True
        curr = parent

    return False


async def is_patient_owner(
    session: AsyncSession,
    consultation: Consultation,
    principal: Principal | None,
    consultation_token_str: str | None,
) -> bool:
    """Verifica pertenencia del paciente a la consulta (por token o por cuenta)."""
    if consultation_token.is_valid_for(consultation_token_str, consultation.id):
        return True
    if principal is not None and not principal.is_staff:
        patient = await session.get(Patient, consultation.patient_id)
        if patient is not None and patient.user_id == principal.id:
            return True
    return False


# LISTA BLANCA de estados que admiten mensajes (CA2.2). Es blanca y no negra a propósito: con
# una lista negra, cualquier estado nuevo del enum (`CONSULTATION_STATUSES`) nacería escribible
# sin que nadie lo decidiera — `urgent_in_person` y `contacted_whatsapp` pasaban así.
#
# `contacted_whatsapp` está dentro por decisión del cliente (2026-10-05) y no por CA2.2, que no
# lo menciona: es un caso ABIERTO con médico asignado (ver `_OPEN_ASSIGNED_STATUSES` en
# `services/consultations.py`) y describe justo al paciente al que el médico tuvo que dar su
# número personal — o sea, el escenario que este módulo existe para reemplazar.
# `urgent_in_person` sigue FUERA: ahí la vía es la atención presencial, no el seguimiento escrito.
_WRITABLE_STATUSES = frozenset(
    {"in_progress", "scheduled", "referred_to_specialist", "contacted_whatsapp"}
)
# Cerrada: admite mensajes solo dentro de MESSAGING_AFTER_CLOSE_HOURS desde el cierre.
_CLOSED_STATUSES = frozenset({"closed", "cancelled", "patient_no_show", "closed_by_admin"})
# `waiting` lo escribe SOLO el paciente: todavía no hay médico tratante, así que para el médico
# no es "su" hilo (y para el paciente es la sala de espera, donde sí puede escribir).
_PATIENT_ONLY_STATUSES = frozenset({"waiting"})

_NO_MESSAGES_DETAIL = "Esta consulta ya no admite mensajes."


def check_can_write_in_consultation(consultation: Consultation, is_doctor: bool) -> None:
    """Valida si el estado y la ventana de la consulta admiten nuevos mensajes (CA2.2/CA4.5).

    Lanza `ConflictError` (409) para todo estado que no esté en la lista blanca.
    """
    status = consultation.status

    if status in _WRITABLE_STATUSES:
        return

    if status in _PATIENT_ONLY_STATUSES:
        if is_doctor:
            raise ConflictError("La consulta aún no ha sido tomada por un médico.")
        return

    if status in _CLOSED_STATUSES:
        closed_reference = (
            consultation.closed_at or consultation.ended_at or consultation.created_at
        )
        if closed_reference is None:
            raise ConflictError(_NO_MESSAGES_DETAIL)
        if closed_reference.tzinfo is None:
            closed_reference = closed_reference.replace(tzinfo=UTC)
        limit = closed_reference + timedelta(hours=settings.MESSAGING_AFTER_CLOSE_HOURS)
        if datetime.now(UTC) > limit:
            raise ConflictError(_NO_MESSAGES_DETAIL)
        return

    raise ConflictError(_NO_MESSAGES_DETAIL)


def validate_attachment_file(
    filename: str, content: bytes, content_type: str | None
) -> tuple[str, str]:
    """Valida extensión, magic bytes y tamaño del archivo.

    Rechaza estrictamente GIF con UnprocessableError (HTTP 422).
    Devuelve (mime_type_validado, clean_filename).
    """
    if not filename:
        raise UnprocessableError("El nombre del archivo es requerido.")
    clean_name = Path(filename).name.strip()
    if not clean_name:
        raise UnprocessableError("Nombre de archivo inválido.")

    # 1. Prohibición estricta de GIF (R15.2)
    lower_name = clean_name.lower()
    if lower_name.endswith(".gif") or (content_type and content_type.lower() == "image/gif"):
        raise UnprocessableError("El formato GIF no está permitido.")
    if content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
        raise UnprocessableError("El formato GIF no está permitido.")

    # 2. Tamaño
    if len(content) == 0:
        raise UnprocessableError("El archivo no puede estar vacío.")
    if len(content) > settings.MESSAGING_MAX_ATTACHMENT_SIZE_BYTES:
        raise UnprocessableError(
            f"El archivo excede el tamaño máximo permitido de "
            f"{settings.MESSAGING_MAX_ATTACHMENT_SIZE_BYTES // (1024 * 1024)} MB."
        )

    # 3. Magic bytes
    detected_mime: str | None = None
    if content.startswith(b"%PDF"):
        detected_mime = "application/pdf"
    elif content.startswith(b"\xff\xd8\xff"):
        detected_mime = "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        detected_mime = "image/png"
    elif content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP":
        detected_mime = "image/webp"

    if detected_mime is None or detected_mime not in settings.messaging_allowed_mime_types:
        raise UnprocessableError("Formato de archivo no admitido o contenido no válido.")

    return detected_mime, clean_name


async def upload_attachment(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    filename: str,
    content: bytes,
    content_type: str | None,
    principal: Principal | None,
    consultation_token_str: str | None,
    client_ip: str | None = None,
) -> tuple[MessageAttachment, ClinicalGrant]:
    """Sube un archivo adjunto, valida magic bytes y lo persiste cifrado."""
    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")

    # Permisos de subida: tratante o paciente dueño
    if principal is not None and principal.is_staff:
        if not await is_doctor_in_chain(session, consultation, principal):
            raise NotFoundError("Consulta no encontrada.")
        uploader_role = "doctor"
        uploader_user_id = principal.id
        grant = treating_grant("assigned_doctor")
    else:
        if not await is_patient_owner(session, consultation, principal, consultation_token_str):
            raise NotFoundError("Consulta no encontrada.")
        uploader_role = "patient"
        uploader_user_id = principal.id if (principal and not principal.is_staff) else None
        grant = summary_grant("patient_owner")
        record_patient_presence(consultation.id)
        consultation.patient_last_seen_at = datetime.now(UTC)

    check_can_write_in_consultation(consultation, is_doctor=(uploader_role == "doctor"))

    mime_type, clean_name = validate_attachment_file(filename, content, content_type)

    attachment_id = uuid.uuid4()
    storage_path = f"consultations/{consultation.id}/attachments/{attachment_id}.bin"
    await storage.save_attachment_file(storage_path, content, content_type=mime_type)

    attachment = MessageAttachment(
        id=attachment_id,
        consultation_id=consultation.id,
        message_id=None,
        uploader_role=uploader_role,
        uploader_user_id=uploader_user_id,
        file_name=clean_name,
        mime_type=mime_type,
        file_size_bytes=len(content),
        storage_path=storage_path,
        created_at=datetime.now(UTC),
    )
    session.add(attachment)

    await log_action(
        session,
        action="attachment.uploaded",
        actor_user_id=uploader_user_id,
        resource="message_attachments",
        resource_id=str(attachment.id),
        metadata={
            "consultation_id": str(consultation.id),
            "mime_type": mime_type,
            "size": len(content),
        },
        ip=client_ip,
        correlation_id=correlation_id_ctx.get(),
    )
    await session.commit()
    await session.refresh(attachment)
    return attachment, grant


async def get_attachment_for_download(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    attachment_id: uuid.UUID,
    principal: Principal | None,
    consultation_token_str: str | None,
    client_ip: str | None = None,
) -> tuple[bytes, str, str]:
    """Valida grant clínico y devuelve el binario, nombre de archivo y tipo MIME."""
    attachment = await session.get(MessageAttachment, attachment_id)
    if attachment is None or attachment.consultation_id != consultation_id:
        raise NotFoundError("Adjunto no encontrado.")

    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")

    # Evaluación de grant clínico (admin NO puede descargar archivos clínicos)
    grant: ClinicalGrant | None = None
    if principal is not None and principal.is_staff:
        if await is_doctor_in_chain(session, consultation, principal):
            grant = treating_grant("assigned_doctor")
        elif principal.is_admin:
            # Ser admin sin ser el médico tratante no concede acceso a adjuntos clínicos
            raise ForbiddenError("Los administradores no tienen acceso a adjuntos clínicos.")
        else:
            raise NotFoundError("Consulta no encontrada.")
    elif await is_patient_owner(session, consultation, principal, consultation_token_str):
        grant = summary_grant("patient_owner")
        record_patient_presence(consultation.id)
    else:
        raise NotFoundError("Consulta no encontrada.")

    data = await storage.get_attachment_file(attachment.storage_path)
    if data is None:
        raise NotFoundError("El archivo no se encuentra en el almacenamiento.")

    clean_filename = reveal(attachment.file_name) or "archivo"

    await audit_clinical_read(
        session,
        principal=principal,
        ip=client_ip,
        resource="message_attachments",
        grants=[(attachment.id, grant)],
    )

    return data, clean_filename, attachment.mime_type


async def _find_by_client_msg_id(
    session: AsyncSession, consultation_id: uuid.UUID, client_msg_id: str
) -> Message | None:
    """Busca el mensaje ya persistido con ese `client_msg_id` en el hilo (idempotencia)."""
    return await session.scalar(
        select(Message)
        .where(
            Message.consultation_id == consultation_id,
            Message.client_msg_id == client_msg_id,
        )
        .options(selectinload(Message.attachments))
    )


async def send_message(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    data: MessageCreate,
    principal: Principal | None,
    consultation_token_str: str | None,
    client_ip: str | None = None,
) -> tuple[Message, ClinicalGrant | None, bool]:
    """Envía un mensaje en el hilo de una consulta.

    Devuelve (Message, grant, created: bool). Si ya existe por client_msg_id, created es False.
    """
    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")

    is_doctor = principal is not None and principal.is_staff
    if is_doctor:
        if not await is_doctor_in_chain(session, consultation, principal):
            raise NotFoundError("Consulta no encontrada.")
        sender_role = "doctor"
        direction = "doctor_to_patient"
        sender_user_id = principal.id
        grant = treating_grant("assigned_doctor")
    else:
        if not await is_patient_owner(session, consultation, principal, consultation_token_str):
            raise NotFoundError("Consulta no encontrada.")
        sender_role = "patient"
        direction = "patient_to_doctor"
        sender_user_id = principal.id if (principal and not principal.is_staff) else None
        grant = summary_grant("patient_owner")
        record_patient_presence(consultation.id)
        consultation.patient_last_seen_at = datetime.now(UTC)

    check_can_write_in_consultation(consultation, is_doctor=is_doctor)

    # Límite de mensajes por hora para pacientes (CA4.4)
    if sender_role == "patient":
        one_hour_ago = datetime.now(UTC) - timedelta(hours=1)
        patient_hourly_count = await session.scalar(
            select(func.count(Message.id)).where(
                Message.consultation_id == consultation.id,
                Message.sender_role == "patient",
                Message.sent_at >= one_hour_ago,
            )
        )
        if (
            patient_hourly_count is not None
            and patient_hourly_count >= settings.MESSAGING_PATIENT_HOURLY_LIMIT
        ):
            raise ConflictError("Límite de mensajes por hora alcanzado para esta consulta.")

    # Idempotencia por client_msg_id: primero el camino barato (ya existe).
    if data.client_msg_id:
        existing = await _find_by_client_msg_id(session, consultation.id, data.client_msg_id)
        if existing is not None:
            return existing, grant, False

    # Validar y vincular adjuntos
    attachments: list[MessageAttachment] = []
    if data.attachment_ids:
        stmt = select(MessageAttachment).where(
            MessageAttachment.id.in_(data.attachment_ids),
            MessageAttachment.consultation_id == consultation.id,
        )
        attachments = list((await session.execute(stmt)).scalars().all())
        if len(attachments) != len(data.attachment_ids):
            raise BadRequestError("Uno o más adjuntos no existen o no pertenecen a esta consulta.")

    kind = "attachment" if (not data.body and data.attachment_ids) else "text"

    message = Message(
        id=uuid.uuid4(),
        consultation_id=consultation.id,
        sender_role=sender_role,
        sender_user_id=sender_user_id,
        direction=direction,
        channel="web",
        kind=kind,
        body=data.body,
        client_msg_id=data.client_msg_id,
        sent_at=datetime.now(UTC),
        delivery_status="sent",
    )

    # El SELECT de arriba no cierra la carrera: dos envíos simultáneos con el mismo
    # client_msg_id pasan ambos el chequeo y el segundo INSERT choca con el índice único
    # parcial `uq_messages_consultation_client_msg`. Sin este savepoint, eso era un 500 (y
    # dejaba la transacción abortada); con él se vuelve atrás solo el INSERT y se devuelve el
    # mensaje que ganó la carrera, que es lo que el cliente pedía al mandar un client_msg_id.
    try:
        async with session.begin_nested():
            session.add(message)
            await session.flush()
    except IntegrityError:
        if data.client_msg_id:
            winner = await _find_by_client_msg_id(session, consultation.id, data.client_msg_id)
            if winner is not None:
                return winner, grant, False
        raise

    for att in attachments:
        att.message_id = message.id

    await log_action(
        session,
        action="message.sent",
        actor_user_id=sender_user_id,
        resource="messages",
        resource_id=str(message.id),
        metadata={
            "consultation_id": str(consultation.id),
            "direction": direction,
            "has_attachments": bool(data.attachment_ids),
        },
        ip=client_ip,
        correlation_id=correlation_id_ctx.get(),
    )
    await session.commit()

    # Recargar con adjuntos poblados
    reloaded = await session.scalar(
        select(Message).where(Message.id == message.id).options(selectinload(Message.attachments))
    )
    return reloaded or message, grant, True


# Cuerpo del mensaje de sistema que anuncia la videollamada (CA16.5/CA16.6). Es SOLO el texto del
# aviso: ninguna URL y ningún token.
#
# Llevaba el enlace de `/entrar-videoconsulta` con un token de consulta fresco, y eso es un
# secreto de 24 h de vida escrito en texto legible dentro del historial clínico y a la vista en
# el hilo — visible en cualquier captura o pantalla compartida, y expuesto en cuanto el cliente
# no puede validar que el origen coincide (en desarrollo nunca coincide: `FRONTEND_URL` apunta a
# producción y el navegador está en localhost). Quien lee este mensaje ya está autenticado para
# estar en el hilo, así que la interfaz arma el acceso con el `consultation_id` y su propia
# credencial y lo pinta como un botón; aquí no se emite ningún token (uno menos en circulación).
#
# `build_join_url` sigue siendo el camino correcto para `video_ready_email`: ahí el destinatario
# NO está autenticado y el enlace tokenizado es lo único que lo deja entrar. No se toca.
_CALL_STARTED_BODY = "El médico inició la videoconsulta."


async def start_video_call(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    principal: Principal | None,
    client_ip: str | None = None,
) -> tuple[str, uuid.UUID]:
    """Inicia la videollamada del hilo y deja constancia con un mensaje de sistema (R16).

    Asimetría dura (CA16.3): llama **solo** el médico tratante, actual o previo en la cadena
    (mismo criterio que el resto del hilo, `is_doctor_in_chain`). Todo el resto —paciente con
    sesión, paciente con token de consulta, médico ajeno, admin no tratante— recibe
    `NotFoundError`, nunca un 403: un 403 confirmaría que la consulta existe.

    No se dispara `video_ready_email`: el paciente se entera por el mensaje del hilo (el botón
    solo se habilita con el paciente en línea), así el correo sería redundante y gastaría el
    rate limit de Mailtrap.

    Devuelve `(room_url, message_id)`.
    """
    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")

    # El paciente nunca inicia la llamada: ni con sesión propia ni con `X-Consultation-Token`.
    if principal is None or not principal.is_staff:
        raise NotFoundError("Consulta no encontrada.")
    if not await is_doctor_in_chain(session, consultation, principal):
        raise NotFoundError("Consulta no encontrada.")

    check_can_write_in_consultation(consultation, is_doctor=True)

    # Sala idempotente: si ya existe se reutiliza (`ensure_video_room`). Dos clics no dejan a
    # médico y paciente en salas distintas. No se duplica esa lógica aquí.
    consultation = await consultations_service.ensure_video_room(session, consultation.id)
    room_url = consultation.video_room_url
    if not room_url:  # pragma: no cover - ensure_video_room ya falla con ConflictError
        raise ConflictError("La consulta ya no está abierta.")

    # `direction='system'` no es ninguna de las dos direcciones, así que este mensaje NO cuenta
    # como no leído para nadie (ni para el médico que lo provocó ni para el paciente): los
    # contadores y el marcado de leído siguen mirando solo `doctor_to_patient` /
    # `patient_to_doctor`. Decisión deliberada y simétrica (ver spec R16 en tasks/).
    message_id = uuid.uuid4()
    message = Message(
        id=message_id,
        consultation_id=consultation.id,
        sender_role="system",
        sender_user_id=None,
        direction="system",
        channel="web",
        kind="call",
        # `call_session_id` se queda nulo a propósito: la columna apunta a una tabla que no
        # existe (deuda declarada) y R16 excluye el ciclo de vida de la llamada.
        body=_CALL_STARTED_BODY,
        sent_at=datetime.now(UTC),
        delivery_status="sent",
    )
    session.add(message)

    # Sin contenido y sin la URL de la sala (CA16.7).
    await log_action(
        session,
        action="call.started",
        actor_user_id=principal.id,
        resource="consultations",
        resource_id=str(consultation.id),
        metadata={"consultation_id": str(consultation.id), "message_id": str(message_id)},
        ip=client_ip,
        correlation_id=correlation_id_ctx.get(),
    )
    await session.commit()

    return room_url, message_id


async def list_messages(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    principal: Principal | None,
    consultation_token_str: str | None,
    limit: int = 50,
    offset: int = 0,
    after_id: uuid.UUID | None = None,
    before_id: uuid.UUID | None = None,
    client_ip: str | None = None,
) -> tuple[list[Message], int, ClinicalGrant | None]:
    """Lista los mensajes de una consulta con grant clínico.

    Devuelve (messages, unread_count, grant).
    """
    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")

    grant: ClinicalGrant | None = None
    is_doctor = principal is not None and principal.is_staff
    if is_doctor:
        if await is_doctor_in_chain(session, consultation, principal):
            grant = treating_grant("assigned_doctor")
        elif principal.is_admin:
            # Admin sin ser médico del caso ve metadatos pero NO cuerpos (grant = None)
            grant = None
        else:
            raise NotFoundError("Consulta no encontrada.")
    elif await is_patient_owner(session, consultation, principal, consultation_token_str):
        grant = summary_grant("patient_owner")
        record_patient_presence(consultation.id)
    else:
        raise NotFoundError("Consulta no encontrada.")

    # Dirección contraria para marcar entrega y contar no leídos
    opposing_direction = "patient_to_doctor" if is_doctor else "doctor_to_patient"

    # Marcar delivered_at en mensajes contrarios
    if is_doctor or (grant is not None):
        await session.execute(
            update(Message)
            .where(
                Message.consultation_id == consultation.id,
                Message.direction == opposing_direction,
                Message.delivered_at.is_(None),
            )
            .values(delivered_at=func.now())
        )

    # Conteo de no leídos para el llamante
    unread_count = (
        await session.scalar(
            select(func.count(Message.id)).where(
                Message.consultation_id == consultation.id,
                Message.direction == opposing_direction,
                Message.read_at.is_(None),
            )
        )
        or 0
    )

    # Consulta de mensajes
    stmt = (
        select(Message)
        .where(Message.consultation_id == consultation.id)
        .order_by(Message.sent_at.asc(), Message.id.asc())
        .options(selectinload(Message.attachments))
    )

    if after_id:
        after_msg = await session.get(Message, after_id)
        if after_msg:
            stmt = stmt.where(
                (Message.sent_at > after_msg.sent_at)
                | ((Message.sent_at == after_msg.sent_at) & (Message.id > after_msg.id))
            )
    if before_id:
        before_msg = await session.get(Message, before_id)
        if before_msg:
            stmt = stmt.where(
                (Message.sent_at < before_msg.sent_at)
                | ((Message.sent_at == before_msg.sent_at) & (Message.id < before_msg.id))
            )

    stmt = stmt.limit(min(limit, 100)).offset(offset)
    result = await session.execute(stmt)
    messages = list(result.scalars().all())

    # Auditoría clínica si hubo grant
    if grant is not None:
        await audit_clinical_read(
            session,
            principal=principal,
            ip=client_ip,
            resource="messages",
            grants=[(consultation.id, grant)],
        )

    await session.commit()
    return messages, unread_count, grant


async def mark_as_read(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    principal: Principal | None,
    consultation_token_str: str | None,
) -> int:
    """Marca como leídos los mensajes no leídos de la otra dirección (idempotente)."""
    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")

    is_doctor = principal is not None and principal.is_staff
    if is_doctor:
        if await is_doctor_in_chain(session, consultation, principal):
            opposing_direction = "patient_to_doctor"
        elif principal.is_admin:
            raise ForbiddenError("Los administradores no marcan mensajes como leídos.")
        else:
            raise NotFoundError("Consulta no encontrada.")
    elif await is_patient_owner(session, consultation, principal, consultation_token_str):
        opposing_direction = "doctor_to_patient"
        record_patient_presence(consultation.id)
    else:
        raise NotFoundError("Consulta no encontrada.")

    stmt = (
        update(Message)
        .where(
            Message.consultation_id == consultation.id,
            Message.direction == opposing_direction,
            Message.read_at.is_(None),
        )
        .values(read_at=func.now())
    )
    result = await session.execute(stmt)
    marked = result.rowcount
    await session.commit()

    reader_role = "doctor" if is_doctor else "patient"
    notifications.clear_mail_debounce(consultation.id, reader_role)

    return marked


def _is_or_was_treating(principal: Principal):
    """Filtro de pertenencia del buzón: el llamante ES o FUE el tratante del hilo (CA7.1).

    Se aplica **también al admin**: `/inbox` no es la herramienta de supervisión (para eso
    están `consultations.read` y los metadatos del caso, CA3.2). Sin este filtro el admin
    recibía el buzón de toda la plataforma, incluida la presencia del paciente
    (`patient_online` / `patient_last_seen_at`), que la spec concede al médico tratante.

    **Por qué esto y `is_doctor_in_chain` son dos funciones y no una.** Responden preguntas
    distintas y, sobre todo, en lenguajes distintos: esto es una expresión SQL que entra en el
    `WHERE` de una consulta agregada (no puede recorrer la cadena en Python, ni abrir una
    consulta por fila), y aquélla es un predicado `async` que sí la recorre entera. Además lo
    que deciden no es lo mismo: esto decide **qué hilos se LISTAN** (metadatos y contadores, sin
    un solo cuerpo), y `is_doctor_in_chain` decide **quién LEE la conversación y escribe en
    ella** — por eso solo aquélla exige ejercer como médico habilitado y mira el evento del
    claim. Consecuencia conocida y aceptada: un médico puede tener grant sobre un hilo de un
    tramo lejano de la cadena que su buzón no lista (este filtro sube un solo nivel; subir la
    cadena completa en SQL pide un CTE recursivo y no hace falta para el buzón).
    """
    return (Consultation.assigned_doctor_id == principal.id) | (
        Consultation.parent_consultation_id.in_(
            select(Consultation.id).where(Consultation.assigned_doctor_id == principal.id)
        )
    )


async def get_inbox(
    session: AsyncSession,
    principal: Principal,
    only_unread: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[InboxThreadResponse], int]:
    """Lista las conversaciones activas para el buzón del médico con presencia asimétrica."""
    unread_filter = (Message.direction == "patient_to_doctor") & Message.read_at.is_(None)

    stmt = (
        select(
            Consultation.id,
            Consultation.code,
            Consultation.status,
            Consultation.patient_last_seen_at,
            Patient.full_name.label("patient_name"),
            Specialty.name.label("specialty"),
            func.max(Message.sent_at).label("last_message_at"),
            func.count(case((unread_filter, 1))).label("unread_count"),
        )
        .join(Patient, Patient.id == Consultation.patient_id)
        .outerjoin(Specialty, Specialty.id == Consultation.specialty_id)
        .join(Message, Message.consultation_id == Consultation.id)
        .where(_is_or_was_treating(principal))
    )

    stmt = stmt.group_by(
        Consultation.id,
        Consultation.code,
        Consultation.status,
        Consultation.patient_last_seen_at,
        Patient.full_name,
        Specialty.name,
    )

    if only_unread:
        stmt = stmt.having(func.count(case((unread_filter, 1))) > 0)

    # Subquery para total
    subq = stmt.subquery()
    total = (await session.execute(select(func.count()).select_from(subq))).scalar() or 0

    stmt = (
        stmt.order_by(func.max(Message.sent_at).desc(), Consultation.id.desc())
        .offset(offset)
        .limit(min(limit, 100))
    )
    rows = (await session.execute(stmt)).all()

    items: list[InboxThreadResponse] = []
    for row in rows:
        cid = row.id
        latest_msg = await session.scalar(
            select(Message)
            .where(Message.consultation_id == cid)
            .order_by(Message.sent_at.desc(), Message.id.desc())
            .limit(1)
        )
        last_direction = latest_msg.direction if latest_msg else "patient_to_doctor"
        patient_online = is_patient_online(cid, row.patient_last_seen_at)

        items.append(
            InboxThreadResponse(
                consultation_id=cid,
                code=row.code,
                specialty=row.specialty,
                patient_name=row.patient_name,
                status=row.status,
                last_message_at=row.last_message_at,
                last_direction=last_direction,
                unread_count=row.unread_count,
                patient_online=patient_online,
                patient_last_seen_at=row.patient_last_seen_at,
                active_call=None,
            )
        )

    return items, total


async def get_latest_patient_message_signal(
    session: AsyncSession, consultation_id: uuid.UUID
) -> tuple[uuid.UUID, str, datetime, int] | None:
    """Obtiene el último mensaje dirigido al paciente para el stream SSE de la sala (R8.1).

    Devuelve (message_id, direction, sent_at, unread_count) o None si no hay mensajes.
    """
    stmt = (
        select(Message.id, Message.direction, Message.sent_at)
        .where(
            Message.consultation_id == consultation_id,
            Message.direction == "doctor_to_patient",
        )
        .order_by(Message.sent_at.desc(), Message.id.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None

    unread_count = (
        await session.scalar(
            select(func.count(Message.id)).where(
                Message.consultation_id == consultation_id,
                Message.direction == "doctor_to_patient",
                Message.read_at.is_(None),
            )
        )
    ) or 0
    return (row[0], row[1], row[2], unread_count)


async def get_inbox_signal(
    session: AsyncSession, principal: Principal
) -> tuple[int, dict[uuid.UUID, tuple[datetime | None, int]]]:
    """Obtiene total de no leídos y mapa de hilos para el stream SSE del buzón del médico (R8.2).

    Devuelve (total_unread, {consultation_id: (last_message_at, unread_count)}).
    """
    unread_filter = (Message.direction == "patient_to_doctor") & Message.read_at.is_(None)

    stmt = select(
        Consultation.id,
        func.max(Message.sent_at).label("last_message_at"),
        func.count(case((unread_filter, 1))).label("unread_count"),
    ).join(Message, Message.consultation_id == Consultation.id)

    # Mismo filtro de pertenencia que `get_inbox`, admin incluido (CA7.1).
    stmt = stmt.where(_is_or_was_treating(principal))

    stmt = stmt.group_by(Consultation.id)
    rows = (await session.execute(stmt)).all()

    threads: dict[uuid.UUID, tuple[datetime | None, int]] = {}
    unread_total = 0
    for r in rows:
        threads[r.id] = (r.last_message_at, r.unread_count)
        unread_total += r.unread_count

    return unread_total, threads
