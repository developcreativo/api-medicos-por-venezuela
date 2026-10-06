"""Endpoints REST para el módulo de mensajería médico ↔ paciente."""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from email.parser import BytesParser
from email.policy import default
from typing import Any
from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import settings
from src.core.errors import UnprocessableError
from src.core.observability import client_ip
from src.core.ratelimit import limiter
from src.core.security import (
    Principal,
    get_optional_principal,
    require_permission,
)
from src.db.session import get_db, get_session_factory
from src.schemas.clinical import clinical_context
from src.schemas.message import (
    AttachmentResponse,
    InboxThreadResponse,
    MessageCreate,
    MessageResponse,
    MessagesThreadResponse,
    ReadReceiptResponse,
    VideoCallStartResponse,
)
from src.services import messaging, notifications

logger = logging.getLogger("mpv.messaging")

router = APIRouter(tags=["Mensajería"])

tag_metadata = [
    {
        "name": "Mensajería",
        "description": "Buzón e hilos de chat médico ↔ paciente con cifrado clínico.",
    }
]

_CONSULTATION_TOKEN_HEADER = "X-Consultation-Token"


@router.get(
    "/inbox",
    response_model=list[InboxThreadResponse],
    summary="Buzón de mensajes para el médico",
    responses={
        403: {"description": "Permiso insuficiente (requiere messages.read)."},
    },
)
async def get_inbox(
    only_unread: bool = Query(
        default=False, description="Filtrar solo hilos con mensajes no leídos"
    ),
    limit: int = Query(default=50, ge=1, le=100, description="Límite de conversaciones"),
    offset: int = Query(default=0, ge=0, description="Desplazamiento para paginación"),
    principal: Principal = Depends(require_permission("messages.read")),
    db: AsyncSession = Depends(get_db),
) -> list[InboxThreadResponse]:
    """Lista las conversaciones activas para el buzón del médico.

    Incluye estado de presencia asimétrica del paciente (`patient_online`, `patient_last_seen_at`).
    """
    items, _ = await messaging.get_inbox(
        db,
        principal=principal,
        only_unread=only_unread,
        limit=limit,
        offset=offset,
    )
    return items


@router.get(
    "/inbox/stream",
    summary="Stream SSE del buzón del médico (R8.2)",
    responses={
        403: {"description": "Permiso insuficiente (requiere messages.read)."},
    },
)
async def inbox_stream(
    principal: Principal = Depends(require_permission("messages.read")),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> StreamingResponse:
    """Stream SSE de actualizaciones del buzón del médico.

    Emite eventos `inbox` { unread_total, updated: [consultation_id] } cuando cambia el estado.
    Sin cuerpos clínicos ni datos privados (CA8.3).
    """

    async def sse_inbox_events() -> AsyncIterator[str]:
        started = time.monotonic()
        last_unread_total: int | None = None
        last_threads: dict[uuid.UUID, Any] | None = None
        last_emit = started
        yield "retry: 5000\n\n"

        while True:
            try:
                async with session_factory() as session:
                    unread_total, current_threads = await messaging.get_inbox_signal(
                        session, principal
                    )
            except Exception:
                logger.warning("SSE:inbox_stream_error user_id=%s", principal.id, exc_info=True)
                return

            now = time.monotonic()
            if (
                last_threads is None
                or current_threads != last_threads
                or unread_total != last_unread_total
            ):
                if last_threads is None:
                    updated_ids = list(current_threads.keys())
                else:
                    updated_ids = [
                        cid for cid, val in current_threads.items() if last_threads.get(cid) != val
                    ]
                payload = {
                    "unread_total": unread_total,
                    "updated": [str(c) for c in updated_ids],
                }
                yield f"event: inbox\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
                last_unread_total = unread_total
                last_threads = current_threads
                last_emit = now

            if now - last_emit >= settings.WAITING_ROOM_HEARTBEAT_SECONDS:
                yield ": ping\n\n"
                last_emit = now

            if now - started >= settings.WAITING_ROOM_STREAM_MAX_SECONDS:
                return

            await asyncio.sleep(settings.WAITING_ROOM_POLL_SECONDS)

    return StreamingResponse(
        sse_inbox_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/consultations/{consultation_id}/messages",
    response_model=MessagesThreadResponse,
    summary="Listar mensajes de un hilo de consulta",
    responses={
        401: {"description": "No autenticado (sin sesión ni token de consulta)."},
        403: {"description": "No autorizado para esta consulta."},
        404: {"description": "Consulta no encontrada."},
    },
)
async def list_messages(
    consultation_id: uuid.UUID,
    response: Response,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    after_id: uuid.UUID | None = Query(default=None),
    before_id: uuid.UUID | None = Query(default=None),
    x_consultation_token: str | None = Header(default=None, alias=_CONSULTATION_TOKEN_HEADER),
    principal: Principal | None = Depends(get_optional_principal),
    db: AsyncSession = Depends(get_db),
) -> MessagesThreadResponse:
    """Lista los mensajes de la consulta con grant clínico fail-closed.

    Devuelve `{ consultation_id, unread_count, items, clinical_access }`: `unread_count`
    (CA3.4) son los mensajes de la otra dirección sin leer **para el llamante**, y va en el
    cuerpo porque una cabecera no es legible desde el navegador cross-origin sin exponerla.
    El header `X-Unread-Count` se mantiene por compatibilidad, pero el cuerpo manda.

    Sin grant (ej. admin), los cuerpos y nombres de adjuntos salen null.
    """
    if principal is None and not x_consultation_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticación requerida para acceder al hilo de mensajes.",
        )
    if (
        principal is not None
        and principal.is_staff
        and not principal.has_permission("messages.read")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para esta acción.",
        )

    messages, unread_count, grant = await messaging.list_messages(
        db,
        consultation_id=consultation_id,
        principal=principal,
        consultation_token_str=x_consultation_token,
        limit=limit,
        offset=offset,
        after_id=after_id,
        before_id=before_id,
        client_ip=client_ip(request),
    )

    response.headers["X-Unread-Count"] = str(unread_count)

    context = clinical_context(grant)
    return MessagesThreadResponse.model_validate(
        {
            "consultation_id": consultation_id,
            "unread_count": unread_count,
            "items": messages,
        },
        context=context,
    )


@router.post(
    "/consultations/{consultation_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Enviar mensaje en el hilo de una consulta",
    responses={
        200: {"description": "Mensaje ya existente (idempotencia por client_msg_id)."},
        201: {"description": "Mensaje enviado exitosamente."},
        401: {"description": "No autenticado."},
        403: {"description": "Permiso messages.write requerido para staff."},
        404: {"description": "Consulta no encontrada."},
        409: {"description": "Consulta cerrada o no admite mensajes."},
        422: {"description": "Datos de mensaje inválidos."},
        429: {"description": "Demasiados mensajes desde esta IP (`PUBLIC_WRITE_RATE_LIMIT`)."},
    },
)
@limiter.limit(settings.PUBLIC_WRITE_RATE_LIMIT)
async def send_message(
    consultation_id: uuid.UUID,
    data: MessageCreate,
    response: Response,
    request: Request,
    background_tasks: BackgroundTasks,
    x_consultation_token: str | None = Header(default=None, alias=_CONSULTATION_TOKEN_HEADER),
    principal: Principal | None = Depends(get_optional_principal),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Envía un mensaje de texto y/o adjuntos en el hilo de la consulta.

    Idempotente si se proporciona `client_msg_id`.
    """
    if principal is None and not x_consultation_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticación requerida para enviar mensajes.",
        )
    if (
        principal is not None
        and principal.is_staff
        and not principal.has_permission("messages.write")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para esta acción.",
        )

    message, grant, created = await messaging.send_message(
        db,
        consultation_id=consultation_id,
        data=data,
        principal=principal,
        consultation_token_str=x_consultation_token,
        client_ip=client_ip(request),
    )

    if not created:
        response.status_code = status.HTTP_200_OK
    else:
        mail_args = await notifications.message_received_mail_args(
            db,
            consultation_id=consultation_id,
            message_id=message.id,
            direction=message.direction,
        )
        if mail_args:
            background_tasks.add_task(notifications.send_message_notification, **mail_args)

    return MessageResponse.model_validate(message, context=clinical_context(grant))


@router.post(
    "/consultations/{consultation_id}/messages/read",
    response_model=ReadReceiptResponse,
    summary="Marcar mensajes como leídos",
    responses={
        401: {"description": "No autenticado."},
        403: {"description": "No autorizado para esta consulta."},
        404: {"description": "Consulta no encontrada."},
    },
)
async def mark_messages_read(
    consultation_id: uuid.UUID,
    x_consultation_token: str | None = Header(default=None, alias=_CONSULTATION_TOKEN_HEADER),
    principal: Principal | None = Depends(get_optional_principal),
    db: AsyncSession = Depends(get_db),
) -> ReadReceiptResponse:
    """Marca como leídos los mensajes de la otra dirección en este hilo."""
    if principal is None and not x_consultation_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticación requerida.",
        )
    if (
        principal is not None
        and principal.is_staff
        and not principal.has_permission("messages.read")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para esta acción.",
        )

    marked = await messaging.mark_as_read(
        db,
        consultation_id=consultation_id,
        principal=principal,
        consultation_token_str=x_consultation_token,
    )
    return ReadReceiptResponse(marked=marked)


@router.post(
    "/consultations/{consultation_id}/video-call",
    response_model=VideoCallStartResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Iniciar la videollamada desde el hilo (solo el médico tratante)",
    responses={
        201: {
            "description": (
                "Sala asegurada y aviso en el hilo. En una reentrada dentro de la ventana, "
                "`message_id` es el del aviso que ya estaba."
            )
        },
        401: {"description": "No autenticado (sin sesión ni token de consulta)."},
        403: {"description": "Permiso messages.write requerido para staff."},
        404: {
            "description": (
                "Consulta no encontrada, o el llamante no es el médico tratante: médico ajeno, "
                "admin no tratante y paciente (con sesión o con token) reciben 404, nunca 403."
            )
        },
        409: {
            "description": (
                "La consulta no admite mensajes (estado fuera de la lista blanca), ya no está "
                "abierta, está asignada a otro médico, o la cita agendada la abrió otra "
                "petición simultánea."
            )
        },
    },
)
async def start_video_call(
    consultation_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    x_consultation_token: str | None = Header(default=None, alias=_CONSULTATION_TOKEN_HEADER),
    principal: Principal | None = Depends(get_optional_principal),
    db: AsyncSession = Depends(get_db),
) -> VideoCallStartResponse:
    """Inicia la videoconsulta del hilo y deja constancia para el paciente (R16).

    Asegura la sala de la consulta (idempotente: dos clics devuelven la misma URL) y crea en el
    hilo un **mensaje de sistema** (`sender_role=system`, `direction=system`, `kind=call`) con el
    texto del aviso. El paciente lo ve en el hilo sin recargar.

    Es el **único** botón de videoconsulta del detalle (CA16.2b), así que absorbe lo que hacía el
    antiguo «Unirse a videoconsulta»:

    - **Cita agendada:** si la consulta está en `scheduled`, la pasa a `in_progress` y le crea la
      sala antes de responder, con el correo "tu médico ya está en la sala" que ese flujo ya
      enviaba. El doble clic da 409, no abre la cita dos veces.
    - **Reentrada:** volver a entrar dentro de `MESSAGING_CALL_NOTICE_WINDOW_MINUTES` **no**
      añade otro aviso al hilo; se devuelve el `message_id` del que ya está. La sala se asegura y
      el `audit_log` se escribe en cada intento.

    La presencia del paciente **no** condiciona nada (CA16.2): si no está conectado, la llamada se
    inicia igual y el aviso lo espera en el hilo.

    El cuerpo del mensaje **no lleva ninguna URL ni ningún token** (CA16.6): quien lo lee ya está
    autenticado para estar en el hilo, así que la interfaz arma el acceso con el `consultation_id`
    y su propia credencial. `room_url` se devuelve **aquí**, en esta respuesta autenticada, para
    que el médico abra su ventana.

    Solo el médico tratante, actual o previo en la cadena. El paciente **nunca** inicia la
    llamada: con sesión o con `X-Consultation-Token` recibe 404. Queda en `audit_log` como
    `call.started`, sin contenido ni URL de sala.
    """
    if principal is None and not x_consultation_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticación requerida.",
        )
    if (
        principal is not None
        and principal.is_staff
        and not principal.has_permission("messages.write")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para esta acción.",
        )

    room_url, message_id, video_mail_args = await messaging.start_video_call(
        db,
        consultation_id=consultation_id,
        principal=principal,
        client_ip=client_ip(request),
    )
    # Solo cuando esta llamada abrió una cita agendada: es el correo que ya mandaba
    # `POST /consultations/{id}/start`, encolado igual que allí (CA16.2b).
    if video_mail_args:
        background_tasks.add_task(notifications.send_video_ready_email, **video_mail_args)
    return VideoCallStartResponse(room_url=room_url, message_id=message_id)


async def _extract_upload_file(request: Request) -> tuple[str, bytes, str | None]:
    """Extrae el archivo de una petición multipart/form-data usando la biblioteca estándar."""
    content_type_header = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type_header:
        raise UnprocessableError("La petición debe ser multipart/form-data.")

    body = await request.body()
    if not body:
        raise UnprocessableError("El cuerpo de la petición está vacío.")

    raw = f"Content-Type: {content_type_header}\r\n\r\n".encode("latin-1") + body
    msg = BytesParser(policy=default).parsebytes(raw)
    for part in msg.iter_parts():
        filename = part.get_filename()
        if filename:
            content = part.get_payload(decode=True) or b""
            return filename, content, part.get_content_type()

    raise UnprocessableError("No se encontró ningún archivo adjunto en la petición.")


@router.post(
    "/consultations/{consultation_id}/attachments",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Subir archivo adjunto para el chat",
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "file": {
                                "type": "string",
                                "format": "binary",
                                "description": (
                                    "Archivo PDF o imagen (JPG, PNG, WEBP). GIF prohibido."
                                ),
                            }
                        },
                        "required": ["file"],
                    }
                }
            }
        }
    },
    responses={
        201: {"description": "Archivo subido y cifrado exitosamente."},
        401: {"description": "No autenticado."},
        403: {"description": "Permiso messages.write requerido para staff."},
        404: {"description": "Consulta no encontrada."},
        422: {
            "description": (
                "Formato inválido (GIF prohibido), magic bytes no coinciden o excede 10 MB."
            )
        },
        429: {"description": "Demasiadas subidas desde esta IP (`PUBLIC_WRITE_RATE_LIMIT`)."},
    },
)
@limiter.limit(settings.PUBLIC_WRITE_RATE_LIMIT)
async def upload_attachment(
    consultation_id: uuid.UUID,
    request: Request,
    x_consultation_token: str | None = Header(default=None, alias=_CONSULTATION_TOKEN_HEADER),
    principal: Principal | None = Depends(get_optional_principal),
    db: AsyncSession = Depends(get_db),
) -> AttachmentResponse:
    """Sube un archivo adjunto clínico (PDF, JPG, PNG, WEBP).

    Rechaza estrictamente formato GIF con HTTP 422.
    """
    if principal is None and not x_consultation_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticación requerida para subir adjuntos.",
        )
    if (
        principal is not None
        and principal.is_staff
        and not principal.has_permission("messages.write")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para esta acción.",
        )

    filename, content, content_type = await _extract_upload_file(request)

    attachment, grant = await messaging.upload_attachment(
        db,
        consultation_id=consultation_id,
        filename=filename or "archivo",
        content=content,
        content_type=content_type,
        principal=principal,
        consultation_token_str=x_consultation_token,
        client_ip=client_ip(request),
    )

    return AttachmentResponse.model_validate(attachment, context=clinical_context(grant))


@router.get(
    "/consultations/{consultation_id}/attachments/{attachment_id}",
    summary="Descargar o visualizar archivo adjunto",
    responses={
        200: {"description": "Binario del archivo con cabeceras nosniff e inline."},
        401: {"description": "No autenticado."},
        403: {"description": "Sin acceso clínico al archivo (ej. administradores)."},
        404: {"description": "Consulta o adjunto no encontrado."},
    },
)
async def download_attachment(
    consultation_id: uuid.UUID,
    attachment_id: uuid.UUID,
    request: Request,
    x_consultation_token: str | None = Header(default=None, alias=_CONSULTATION_TOKEN_HEADER),
    principal: Principal | None = Depends(get_optional_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Descarga segura de archivo adjunto con grant clínico y nosniff."""
    if principal is None and not x_consultation_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticación requerida.",
        )
    if (
        principal is not None
        and principal.is_staff
        and not principal.has_permission("messages.read")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para esta acción.",
        )

    content, filename, mime_type = await messaging.get_attachment_for_download(
        db,
        consultation_id=consultation_id,
        attachment_id=attachment_id,
        principal=principal,
        consultation_token_str=x_consultation_token,
        client_ip=client_ip(request),
    )

    safe_filename = filename.replace('"', '\\"')
    encoded_filename = quote(filename)

    disposition = f"inline; filename=\"{safe_filename}\"; filename*=UTF-8''{encoded_filename}"
    headers = {
        "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-cache, no-store, must-revalidate",
    }
    return Response(content=content, media_type=mime_type, headers=headers)
