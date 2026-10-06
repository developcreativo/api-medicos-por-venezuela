"""Capa HTTP (delgada) para consultations y sus eventos.

Autorización (replica las RLS):
- Crear consulta: sin sesión (auto-servicio del paciente anónimo), con rate limit.
- Sala de video / entered-call: sin sesión pero con el token de acceso de ESA consulta.
- Leer: staff ve todo; un paciente autenticado solo ve lo suyo (anti-IDOR).
- Actualizar / cerrar / eventos: staff. Eliminar: admin.

Contenido clínico (motivo, notas, motivo de derivación, notas de eventos): cada respuesta se
valida con el permiso clínico del caller sobre ESE caso (`clinical_context(grant)`, ver
`src/services/clinical_access.py`). Sin permiso sale en null con `clinical_access = "none"`
(el admin, siempre); toda lectura concedida queda en `audit_log` como `READ_CLINICAL_DATA`.
"""

import logging
import uuid
from collections.abc import Sequence

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core import consultation_token
from src.core.config import settings
from src.core.errors import ForbiddenError
from src.core.observability import client_ip
from src.core.ratelimit import limiter
from src.core.security import (
    Principal,
    bearer_credentials,
    get_current_principal,
    get_optional_principal,
    require_permission,
)
from src.db.session import get_db, get_session_factory
from src.models.consultation import Consultation
from src.models.patient import Patient
from src.schemas.clinical import ClinicalGrant, clinical_context
from src.schemas.consultation import (
    ChainItem,
    ConsultationClaimRequest,
    ConsultationCloseRequest,
    ConsultationCreate,
    ConsultationCreatedResponse,
    ConsultationDetailResponse,
    ConsultationPanelResponse,
    ConsultationPatientResponse,
    ConsultationResponse,
    ConsultationUpdate,
    DerivationInfo,
    DerivationTargetResponse,
    DeriveRequest,
    PanelConsultationItem,
    PanelWaitingItem,
    QueueGroupResponse,
    ReferToQueueRequest,
    ReminderRunResponse,
    ScheduleFollowUpRequest,
    ScheduleReferralRequest,
    WaitingRoomResponse,
)
from src.schemas.consultation_event import (
    ConsultationEventCreate,
    ConsultationEventResponse,
)
from src.services import (
    clinical_access,
    messaging,
    notifications,
    queue_access,
    registration_mail,
    waiting_room,
)
from src.services import consultations as consultations_service

logger = logging.getLogger("mpv.api")

router = APIRouter(prefix="/consultations", tags=["consultations"])
tag_metadata = [
    {
        "name": "consultations",
        "description": "Casos/consultas y su historial de eventos (auditoría).",
    }
]

_NOT_FOUND = {404: {"description": "Consulta no encontrada."}}
_TOKEN_RESPONSES = {
    401: {"description": "Falta el token de acceso a la sala o no es válido para esta consulta."},
    429: {"description": "Demasiadas peticiones desde esta IP (rate limit)."},
}

# Cabecera y no query param: en la cabecera el token no queda en los logs del servidor ni en el
# `Referer`. Sigue viajando en la URL hasta el frontend (el paciente llega por link), pero de
# ahí al backend ya no.
_CONSULTATION_TOKEN_HEADER = "X-Consultation-Token"

# `resource` de las entradas READ_CLINICAL_DATA de este router.
_AUDIT_RESOURCE = "consultations"


async def _audit_read(
    db: AsyncSession,
    request: Request,
    principal: Principal,
    grants: Sequence[tuple[uuid.UUID, ClinicalGrant | None]],
) -> None:
    """Una entrada de audit por respuesta con los ids leídos (no-op si no se concedió nada)."""
    await clinical_access.audit_clinical_read(
        db,
        principal=principal,
        ip=client_ip(request),
        resource=_AUDIT_RESOURCE,
        grants=grants,
    )


async def _respond[T: BaseModel](
    db: AsyncSession,
    request: Request,
    principal: Principal,
    schema: type[T],
    consultation: Consultation,
    grant: ClinicalGrant | None,
) -> T:
    """Serializa UNA consulta con el permiso clínico del caller y audita si hubo lectura.
    Primero se valida y después se audita: el audit commitea."""
    out = schema.model_validate(consultation, context=clinical_context(grant))
    await _audit_read(db, request, principal, [(consultation.id, grant)])
    return out


def _treating(principal: Principal, consultation: Consultation) -> ClinicalGrant | None:
    """Permiso del médico tratante (asignado y habilitado) sobre el caso, tal como quedó."""
    return clinical_access.treating_doctor_grant(principal, consultation.assigned_doctor_id)


async def _treating_or_admin(
    db: AsyncSession, request: Request, principal: Principal, consultation: Consultation
) -> ClinicalGrant | None:
    """Detalle / cadena / eventos de un caso: el tratante lo ve con notas; el admin recibe 200
    con lo clínico en null (gestiona el caso, no su contenido); cualquier otro, 403 auditado."""
    grant = _treating(principal, consultation)
    if grant is None and not principal.is_admin:
        await clinical_access.audit_clinical_denied(
            db,
            principal=principal,
            ip=client_ip(request),
            resource=_AUDIT_RESOURCE,
            resource_id=consultation.id,
        )
        raise ForbiddenError("Solo el médico que atiende el caso puede verlo.")
    return grant


def _queue_item_grants(
    principal: Principal, scope: queue_access.QueueScope | None, items: Sequence[Consultation]
) -> list[ClinicalGrant | None]:
    """Permiso por fila de una lista de cola: tratante si es suyo, SUMMARY si está en espera sin
    asignar en su cola (`scope` = `clinical_access.queue_grant`), nada en otro caso."""
    return [
        clinical_access.grant_for_queue_item(
            principal,
            scope,
            assigned_doctor_id=c.assigned_doctor_id,
            specialty_id=c.specialty_id,
            status=c.status,
        )
        for c in items
    ]


async def _queue_appointment_email(
    background_tasks: BackgroundTasks, db: AsyncSession, child: Consultation
) -> None:
    """Encola el email "cita agendada" al paciente. Best-effort y fuera de la request: si el
    paciente no tiene email, `appointment_email_args` devuelve None y no se encola nada."""
    args = await notifications.appointment_email_args(db, child)
    if args:
        background_tasks.add_task(notifications.send_appointment_email, **args)


async def _queue_new_patient_alert(
    background_tasks: BackgroundTasks, db: AsyncSession, consultation: Consultation
) -> None:
    """Encola el aviso a operación de que entró un paciente a la cola.

    Los args se resuelven AQUÍ, con la sesión todavía viva: el BackgroundTask corre tras cerrar
    la request y allí ya no se puede consultar la base. `None` = este caso no se avisa (de
    consultorio, agendado, o sin buzones configurados); no es un fallo."""
    args = await registration_mail.new_patient_mail_args(db, consultation)
    if args:
        background_tasks.add_task(registration_mail.send_new_patient_alert, **args)


async def _queue_derivation_email(
    background_tasks: BackgroundTasks, db: AsyncSession, consultation: Consultation
) -> None:
    """Encola el aviso al paciente de que su caso pasó a otra cola (best-effort, fuera de la
    request). Sin correo del paciente no se encola nada."""
    args = await notifications.derivation_mail_args(db, consultation)
    if args:
        background_tasks.add_task(notifications.send_derivation_email, **args)


async def require_consultation_token(
    consultation_id: uuid.UUID,
    x_consultation_token: str | None = Header(default=None, alias=_CONSULTATION_TOKEN_HEADER),
    principal: Principal | None = Depends(get_optional_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Dependencia de `authorize_consultation_access` (ver ahí las reglas)."""
    await authorize_consultation_access(db, consultation_id, x_consultation_token, principal)


async def authorize_consultation_access(
    db: AsyncSession,
    consultation_id: uuid.UUID,
    x_consultation_token: str | None,
    principal: Principal | None,
) -> None:
    """Exige el token de sala de ESTA consulta (hallazgo M3), una sesión de staff, **o** la
    sesión del propio paciente dueño de la consulta.

    Estos endpoints los usan TRES clientes:
    - el paciente anónimo, que llega por link y solo tiene el token;
    - el médico desde el panel, que tiene sesión pero NO el token del paciente (ver
      panel-medico.tsx: crea la sala si el caso llegó sin ella) — exigir solo el token dejaba al
      médico fuera de la consulta que está atendiendo;
    - el paciente con cuenta que vuelve por `/mi-caso`. Ese token se entregó UNA vez, en la URL
      de la sala de espera, y caduca a las 24 h: quien cerró aquella pestaña tiene sesión pero
      no tiene token, y sin esta rama no podía marcar que entró a su propia videoconsulta.

    La sesión del dueño no es una credencial más débil que el token, es más fuerte: el token
    viaja por la URL (historial, `Referer`, capturas compartidas) y la sesión no. La pertenencia
    se comprueba contra `Patient.user_id`, la misma regla anti-IDOR de las lecturas.

    401 y no 403: el llamante es anónimo por diseño, no es que le falten permisos."""
    if principal is not None and principal.is_staff:
        return
    if consultation_token.is_valid_for(x_consultation_token, consultation_id):
        return
    if principal is not None and await consultations_service.belongs_to_patient(
        db, consultation_id, principal.id
    ):
        return
    logger.warning("SEC:consultation_token_invalid consultation_id=%s", consultation_id)
    if not settings.CONSULTATION_TOKEN_REQUIRED:
        # Ventana de cutover: se loguea pero se deja pasar, para no dejar sin sala al frontend
        # viejo mientras se despliegan backend y frontend por separado. Ver la nota en config.
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token de acceso a la consulta ausente, expirado o de otra consulta.",
    )


@router.get(
    "",
    response_model=list[ConsultationDetailResponse] | list[ConsultationPatientResponse],
    summary="Listar consultas",
    responses={403: {"description": "El listado es del equipo de administración."}},
)
async def list_consultations(
    request: Request,
    skip: int = Query(0, ge=0),
    # Cap 200: el monitor admin/pacientes muestra los casos recientes (hasta 200) y filtra/ordena
    # en el cliente. Endpoint solo-staff; el default sigue en 100.
    limit: int = Query(100, ge=1, le=200),
    status_filter: str | None = Query(None, alias="status"),
    patient_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> list[ConsultationDetailResponse] | list[ConsultationPatientResponse]:
    """Staff ve todas las consultas con el paciente anidado (para que el panel admin/pacientes no
    lea `patients` directo) pero **sin contenido clínico**: motivo, notas y antecedentes van en
    null con `clinical_access = "none"`. Un admin que además ejerce recibe, fila a fila, lo que le
    toca como médico (sus casos completos; los de su cola en espera sin asignar, solo el
    motivo; un caso cancelado o cerrado sin médico, nada). Un
    paciente autenticado solo ve las suyas, con su motivo (`summary`) y sin notas del médico.

    El listado con identidad es del **equipo de administración** (es el panel admin). Un médico
    ve sus casos por `/consultations/panel` (cola anonimizada) y el detalle de los que atiende."""
    if principal.is_staff and not principal.is_admin:
        raise ForbiddenError("El listado de consultas es del equipo de administración.")
    consultations = await consultations_service.list_consultations(
        db,
        skip=skip,
        limit=limit,
        status=status_filter,
        patient_id=patient_id,
        viewer_is_staff=principal.is_staff,
        viewer_user_id=principal.id,
    )
    if principal.is_staff:
        # Solo el equipo admin llega acá (los médicos reciben 403 arriba): todos los campos del
        # paciente, incluido el teléfono de emergencia, son para administración. Lo clínico no.
        scope = await clinical_access.queue_grant(db, principal)
        grants = _queue_item_grants(principal, scope, consultations)
        staff_rows = [
            ConsultationDetailResponse.model_validate(c, context=clinical_context(g))
            for c, g in zip(consultations, grants, strict=True)
        ]
        await _audit_read(
            db, request, principal, [(c.id, g) for c, g in zip(consultations, grants, strict=True)]
        )
        return staff_rows
    # El servicio ya filtró por `patients.user_id = caller`: todas son suyas.
    owner = clinical_access.patient_owner_grant()
    patient_rows = [
        ConsultationPatientResponse.model_validate(c, context=clinical_context(owner))
        for c in consultations
    ]
    await _audit_read(db, request, principal, [(c.id, owner) for c in consultations])
    return patient_rows


@router.post(
    "",
    response_model=ConsultationCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear consulta (público)",
    responses={
        400: {"description": "El `patient_id` no existe."},
        422: {"description": "`status` inválido."},
        429: {"description": "Demasiadas consultas desde esta IP (rate limit)."},
    },
)
@limiter.limit(settings.PUBLIC_WRITE_RATE_LIMIT)
async def create_consultation(
    request: Request,
    payload: ConsultationCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> ConsultationCreatedResponse:
    """Crea una consulta en espera. El `code` lo genera la base de datos (trigger).

    Devuelve además el `access_token` de la sala: es la ÚNICA vez que se entrega, porque el
    paciente anónimo no tiene sesión con la que volver a pedirlo. El frontend lo lleva en la
    URL de /sala-espera en lugar del id crudo.

    Sin contenido clínico en la respuesta (`clinical_access = "none"`): el llamante es anónimo y
    el `patient_id` del cuerpo no prueba que la ficha sea suya (el motivo puede salir de sus
    antecedentes).

    `request` es obligatorio para slowapi (lee la IP del cliente), aunque no se use aquí."""
    consultation = await consultations_service.create_consultation(db, payload)
    await _queue_new_patient_alert(background_tasks, db, consultation)
    return ConsultationCreatedResponse(
        **ConsultationResponse.model_validate(
            consultation, context=clinical_context(None)
        ).model_dump(),
        access_token=consultation_token.issue(consultation.id),
    )


# NOTA: debe ir ANTES de "/{consultation_id}" o FastAPI intenta parsear "panel" como UUID (422).
@router.get(
    "/panel",
    response_model=ConsultationPanelResponse,
    summary="Cola del panel médico (espera + mías + cerradas)",
)
async def consultation_panel(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("queue.read")),
) -> ConsultationPanelResponse:
    """Todo lo que el panel del médico necesita en una llamada: la cola de espera (casos sin
    asignar), las consultas abiertas del propio médico y cuántas ha cerrado.

    `queues` son las colas que el panel pinta por separado: una por especialidad del médico (puede
    tener varias) más la de entrada (Medicina general, donde caen los pacientes que no saben qué
    necesitan) si atiende salud física. Con una sola cola el panel muestra la lista directa. Un
    admin las ve todas: si además es especialista se le añade una cola `is_rest` con el resto, y
    si no ejerce ninguna especialidad no recibe colas (una sola lista).

    Contenido clínico por fila (`clinical_access`): en `mine`, `full`; en `waiting`, `summary`
    (motivo, antecedentes, alergias) si el caso está en SUS colas como médico **o si el principal
    es admin** (ve todas las colas y necesita el motivo para triar); `none` (null) en el resto.
    El audit va por petición (una fila por vía de acceso con todos los ids), no por fila del
    panel."""
    waiting, mine, my_closed, scope = await consultations_service.get_panel(
        db,
        principal.id,
        doctor_specialty_id=principal.specialty_id,
        is_admin=principal.is_admin,
    )
    # El alcance de la cola que da SUMMARY es el del principal COMO MÉDICO. Para un no-admin es
    # el mismo que ya calculó el panel; al admin se le calcula aparte (sin la vista global).
    clinical_scope = (
        scope
        if not principal.is_admin and clinical_access.practices_medicine(principal)
        else await clinical_access.queue_grant(db, principal)
    )
    # `panel_queue_grant` y no `grant_for_queue_item`: el admin recibe el motivo de los casos en
    # espera (solo en esta vista; ver el docstring de la función).
    waiting_grants = [
        clinical_access.panel_queue_grant(
            principal,
            clinical_scope,
            assigned_doctor_id=c.assigned_doctor_id,
            specialty_id=c.specialty_id,
            status=c.status,
        )
        for c in waiting
    ]
    mine_grants = [_treating(principal, c) for c in mine]
    response = ConsultationPanelResponse(
        waiting=[
            PanelWaitingItem.model_validate(c, context=clinical_context(g))
            for c, g in zip(waiting, waiting_grants, strict=True)
        ],
        mine=[
            PanelConsultationItem.model_validate(c, context=clinical_context(g))
            for c, g in zip(mine, mine_grants, strict=True)
        ],
        my_closed_count=my_closed,
        queue_blocked_reason=scope.blocked_reason,
        queues=[
            QueueGroupResponse(
                id=g.id,
                name=g.name,
                is_triage=g.is_triage,
                is_rest=g.is_rest,
                specialty_ids=sorted(g.specialty_ids),
            )
            for g in scope.groups
        ],
    )
    await _audit_read(
        db,
        request,
        principal,
        [
            *zip((c.id for c in waiting), waiting_grants, strict=True),
            *zip((c.id for c in mine), mine_grants, strict=True),
        ],
    )
    return response


# NOTA: antes de "/{consultation_id}" por lo mismo que "/panel".
@router.get(
    "/derivation-targets",
    response_model=list[DerivationTargetResponse],
    summary="Especialidades a las que se puede derivar un paciente",
)
async def derivation_targets(
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("queue.read")),
) -> list[DerivationTargetResponse]:
    """Especialidades activas, que no son de relleno ("Otra") y con al menos un médico
    habilitado atendiendo su cola. Es la lista del modal "Derivar a especialista": derivar a una
    cola que nadie mira dejaría al paciente esperando para siempre."""
    targets = await consultations_service.derivation_targets(db)
    return [DerivationTargetResponse.model_validate(t) for t in targets]


# NOTA: debe ir ANTES de "/{consultation_id}" o FastAPI intenta parsear "agenda" como UUID (422).
@router.get(
    "/agenda",
    response_model=list[ConsultationResponse],
    summary="Mi agenda: citas agendadas del médico autenticado",
)
async def my_agenda(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("queue.read")),
) -> list[ConsultationResponse]:
    """Citas AGENDADAS (status 'scheduled') asignadas al médico autenticado, por fecha ascendente.
    El paciente ve las suyas por su propio scoping (list_consultations, viewer_is_staff=False).
    Son casos suyos: el médico habilitado los recibe con notas (`full`); un admin que no ejerce,
    con lo clínico en null."""
    agenda = await consultations_service.list_agenda(db, doctor_user_id=principal.id)
    grants = [_treating(principal, c) for c in agenda]
    rows = [
        ConsultationResponse.model_validate(c, context=clinical_context(g))
        for c, g in zip(agenda, grants, strict=True)
    ]
    await _audit_read(
        db, request, principal, [(c.id, g) for c, g in zip(agenda, grants, strict=True)]
    )
    return rows


@router.post(
    "/agenda/send-due-reminders",
    response_model=ReminderRunResponse,
    summary="Enviar recordatorios de citas próximas (cron externo)",
)
async def send_due_reminders(
    window_minutes: int = Query(30, ge=1, le=1440, description="Ventana en minutos (def. 30)"),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("queue.manage")),
) -> ReminderRunResponse:
    """Envía el recordatorio de las citas agendadas cuya hora cae dentro de la ventana y que aún no
    lo recibieron (idempotente por `reminder_sent_at`). Pensado para un CRON externo que lo llame
    cada 1–5 min. Ver .knowledge/agenda.md."""
    sent = await notifications.send_due_reminders(db, window_minutes)
    return ReminderRunResponse(sent=sent, window_minutes=window_minutes)


@router.get(
    "/{consultation_id}",
    response_model=ConsultationDetailResponse | ConsultationPatientResponse,
    summary="Obtener consulta",
    responses={
        **_NOT_FOUND,
        403: {"description": "Solo el médico que atiende el caso o el equipo admin."},
    },
)
async def get_consultation(
    consultation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> ConsultationDetailResponse | ConsultationPatientResponse:
    """Staff recibe la vista de staff + el paciente anidado, para que el panel no lea `patients`
    directo. Un paciente autenticado solo recibe su propia consulta, con su motivo (`summary`) y
    sin las notas del médico.

    La identidad del paciente (nombre, cédula, contacto) solo la ven el médico asignado al caso y
    el equipo admin: el resto del staff recibe 403 (queda en `audit_log` como intento denegado).
    El contenido clínico (motivo, notas, antecedentes, motivo de derivación) solo va en claro para
    el médico tratante (`full`); el admin recibe esos campos en null (`none`)."""
    consultation = await consultations_service.get_consultation_detail(
        db, consultation_id, viewer_is_staff=principal.is_staff, viewer_user_id=principal.id
    )
    if principal.is_staff:
        grant = await _treating_or_admin(db, request, principal, consultation)
        context = clinical_context(grant)
        # Poblar la relación `patient` explícitamente (evita el lazy-load async) para el detalle.
        consultation.patient = await db.get(Patient, consultation.patient_id)
        response = ConsultationDetailResponse.model_validate(consultation, context=context)
        derivation = await consultations_service.get_derivation(db, consultation)
        if derivation is not None:
            response.derivation = DerivationInfo.model_validate(derivation, context=context)
        # can_view_patient_address: true si el principal está en la allowlist O es el médico
        # asignado a esta consulta. Lo calcula el servidor, no el cliente.
        response.can_view_patient_address = (
            (principal.email or "").lower() in settings.address_viewer_emails
            or consultation.assigned_doctor_id == principal.id
        )
        await _audit_read(db, request, principal, [(consultation.id, grant)])
        return response
    return await _respond(
        db,
        request,
        principal,
        ConsultationPatientResponse,
        consultation,
        clinical_access.patient_owner_grant(),
    )


@router.patch(
    "/{consultation_id}",
    response_model=ConsultationResponse,
    summary="Actualizar consulta (estado / asignación / notas)",
    responses={
        **_NOT_FOUND,
        403: {
            "description": "Motivo o notas clínicas (`chief_complaint`, `clinical_notes`, "
            "`internal_note`) enviados por quien no es el médico que atiende el caso, o "
            "`assigned_doctor_id` = quien llama (tomar un caso es solo por el claim atómico)."
        },
        409: {"description": "La consulta está asignada a otro médico."},
        422: {"description": "`status` inválido."},
    },
)
async def update_consultation(
    consultation_id: uuid.UUID,
    payload: ConsultationUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.write")),
) -> ConsultationResponse:
    """Estado, prioridad, asignación y gestión admin (`nota_admin`, `admin_seguimiento`) los
    cambia el admin o el médico del caso. Motivo y notas clínicas, **solo el médico tratante**:
    si otro (el admin incluido) los manda, 403. El admin asigna el caso a OTRO médico, nunca a sí
    mismo (403): tomarlo es solo por el claim atómico. La respuesta trae lo clínico en claro solo
    si el caller queda como médico tratante del caso; si no, en null."""
    consultation = await consultations_service.update_consultation(
        db,
        consultation_id,
        payload,
        actor_user_id=principal.id,
        actor_is_admin=principal.is_admin,
        actor_practices=clinical_access.practices_medicine(principal),
    )
    return await _respond(
        db,
        request,
        principal,
        ConsultationResponse,
        consultation,
        _treating(principal, consultation),
    )


@router.delete(
    "/{consultation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar consulta (admin)",
    responses=_NOT_FOUND,
)
async def delete_consultation(
    consultation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.delete")),
) -> None:
    await consultations_service.delete_consultation(db, consultation_id, deleted_by=principal.id)


# --- Acciones de negocio (cierre, presencia, videoconsulta) ---


@router.post(
    "/{consultation_id}/close",
    response_model=ConsultationResponse,
    summary="Cerrar consulta o marcar ausencia (staff)",
    responses={
        **_NOT_FOUND,
        403: {"description": "Nota de cierre enviada por quien no es el médico que atiende."},
        409: {"description": "La consulta está asignada a otro médico."},
    },
)
async def close_consultation(
    consultation_id: uuid.UUID,
    payload: ConsultationCloseRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.close")),
) -> ConsultationResponse:
    """Cierra (`closed`) o marca `patient_no_show`, guarda la nota y registra el evento.
    El autor del cierre es el médico autenticado. La nota es clínica: solo la escribe el médico
    tratante (un admin cierra sin nota; con nota, 403). Lo clínico de la respuesta, solo en claro
    para el médico tratante."""
    consultation = await consultations_service.close_consultation(
        db,
        consultation_id,
        payload.outcome,
        closed_by=principal.id,
        note=payload.note,
        signature=payload.signature,
        actor_is_admin=principal.is_admin,
        actor_practices=clinical_access.practices_medicine(principal),
    )
    return await _respond(
        db,
        request,
        principal,
        ConsultationResponse,
        consultation,
        _treating(principal, consultation),
    )


@router.post(
    "/{consultation_id}/schedule-follow-up",
    response_model=ConsultationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Agendar seguimiento: cierra esta consulta (firmada) y crea la hija agendada",
    responses={
        **_NOT_FOUND,
        403: {"description": "Nota de cierre enviada por quien no es el médico que atiende."},
        409: {"description": "La consulta está asignada a otro médico."},
    },
)
async def schedule_follow_up(
    consultation_id: uuid.UUID,
    payload: ScheduleFollowUpRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.close")),
) -> ConsultationResponse:
    """Cierra la consulta actual (firmada) y crea una consulta HIJA agendada para otra fecha,
    continuando la cadena de seguimiento. Devuelve la consulta hija creada (con lo clínico en
    claro solo para el médico tratante, que es quien la sigue). La nota de cierre solo la escribe
    el médico tratante (403 si no)."""
    child = await consultations_service.schedule_follow_up(
        db,
        parent_id=consultation_id,
        scheduled_at=payload.scheduled_at,
        closing_note=payload.closing_note,
        signature=payload.signature,
        actor_user_id=principal.id,
        actor_is_admin=principal.is_admin,
        actor_practices=clinical_access.practices_medicine(principal),
    )
    await _queue_appointment_email(background_tasks, db, child)
    return await _respond(
        db, request, principal, ConsultationResponse, child, _treating(principal, child)
    )


@router.post(
    "/{consultation_id}/refer",
    response_model=ConsultationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Agendar con especialista: entrega esta consulta (derivada) y agenda con otro médico",
    responses={
        **_NOT_FOUND,
        403: {"description": "Quien refiere no es el médico que atiende el caso."},
        409: {"description": "La consulta está asignada a otro médico."},
    },
)
async def refer_to_specialist(
    consultation_id: uuid.UUID,
    payload: ScheduleReferralRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.close")),
) -> ConsultationResponse:
    """Deriva la consulta a OTRO médico: la actual queda 'referred_to_specialist' y se crea una
    hija agendada asignada al especialista, con el motivo firmado. Solo refiere el médico
    tratante (el motivo es clínico). Devuelve la consulta hija: como ya es del especialista, sale
    con lo clínico en null (`clinical_access = "none"`); él la ve completa en su detalle.

    El correo al especialista no lleva el motivo: solo fecha, código de caso y enlace."""
    child = await consultations_service.schedule_referral(
        db,
        parent_id=consultation_id,
        invited_doctor_id=payload.invited_doctor_id,
        scheduled_at=payload.scheduled_at,
        reason=payload.reason,
        signature=payload.signature,
        actor_user_id=principal.id,
        actor_is_admin=principal.is_admin,
        actor_practices=clinical_access.practices_medicine(principal),
    )
    await _queue_appointment_email(background_tasks, db, child)
    # Email "te refirieron una cita" al especialista (si lo tiene habilitado; opt-out). Sin el
    # motivo: el correo sale del sistema y ningún texto clínico viaja por él.
    ref_text = (
        "Un colega te refirió un paciente para una cita.\n\n"
        f"Fecha y hora: {notifications.fmt_when(payload.scheduled_at)}\n"
        f"Código de caso: {child.code}\n\n"
        "Ingresa a tu agenda en Médicos por Venezuela.\n"
    )
    ref_args = await notifications.doctor_event_email_args(
        db,
        user_id=payload.invited_doctor_id,
        event="referral_received",
        subject="Te refirieron un paciente",
        text=ref_text,
    )
    if ref_args:
        background_tasks.add_task(notifications.send_mail, **ref_args)
    return await _respond(
        db, request, principal, ConsultationResponse, child, _treating(principal, child)
    )


@router.get(
    "/{consultation_id}/chain",
    response_model=list[ChainItem],
    summary="Historial de la cadena de seguimiento (padre→hijas) de una consulta",
    responses={
        **_NOT_FOUND,
        403: {"description": "Solo el médico que atiende el caso o el equipo admin."},
    },
)
async def consultation_chain(
    consultation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.read")),
) -> list[ChainItem]:
    """Todas las consultas de la cadena (raíz + descendientes) a la que pertenece esta consulta,
    ordenadas — para ver el historial de seguimiento completo.

    Pertenencia sobre la consulta PEDIDA: su médico tratante la recibe con motivo y notas
    (`full`), y también sus ANCESTROS (el especialista referido necesita las notas previas); el
    admin, la cadena con lo clínico en null; cualquier otro médico, 403 (auditado).

    El acceso se decide eslabón a eslabón: las hijas y las ramas hermanas (p. ej. la derivación
    que tomó otro especialista) salen en null (`clinical_access = "none"`) salvo que quien llama
    sea también el médico tratante de ESE eslabón. Solo se auditan los eslabones concedidos."""
    requested = await consultations_service.get_consultation(db, consultation_id)  # 404
    grant = await _treating_or_admin(db, request, principal, requested)
    chain = await consultations_service.get_chain(db, consultation_id)
    lineage = (
        consultations_service.lineage_ids(chain, consultation_id) if grant is not None else set()
    )
    grants = [grant if c.id in lineage else _treating(principal, c) for c in chain]
    items = [
        ChainItem.model_validate(c, context=clinical_context(g))
        for c, g in zip(chain, grants, strict=True)
    ]
    await _audit_read(
        db, request, principal, [(c.id, g) for c, g in zip(chain, grants, strict=True)]
    )
    return items


@router.post(
    "/{consultation_id}/claim",
    response_model=ConsultationResponse,
    summary="Tomar una consulta en espera (claim atómico)",
    responses={
        **_NOT_FOUND,
        403: {"description": "El caso no corresponde a la especialidad del médico."},
        409: {"description": "La consulta ya fue tomada por otro médico."},
    },
)
async def claim_consultation(
    consultation_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    request: Request,
    payload: ConsultationClaimRequest | None = None,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("queue.take")),
) -> ConsultationResponse:
    """El médico autenticado toma un caso en espera de sus colas. Atómico: si otro médico lo
    tomó primero responde 409 (nunca dos médicos sobre el mismo paciente).

    La atención es **siempre por videoconsulta**: el mismo UPDATE crea la sala si el caso no
    tenía, y al paciente le sale el correo "tu médico ya está en la sala" con el enlace. El
    cuerpo es opcional; `{"via_whatsapp": true}` (el panel anterior) responde 422.

    Al tomarlo pasa a ser su médico tratante: la respuesta trae lo clínico en claro (`full`) si
    ejerce; un admin que no ejerce lo recibe en null.
    """
    del payload  # solo existe para rechazar `via_whatsapp: true` en la validación
    consultation = await consultations_service.claim_consultation(
        db,
        consultation_id,
        doctor_user_id=principal.id,
        doctor_specialty_id=principal.specialty_id,
        is_admin=principal.is_admin,
    )
    video_args = await notifications.video_ready_mail_args(db, consultation)
    if video_args:
        background_tasks.add_task(notifications.send_video_ready_email, **video_args)
    return await _respond(
        db,
        request,
        principal,
        ConsultationResponse,
        consultation,
        _treating(principal, consultation),
    )


@router.post(
    "/{consultation_id}/start",
    response_model=ConsultationResponse,
    summary="Iniciar una cita agendada (scheduled → in_progress + sala)",
    responses={
        **_NOT_FOUND,
        409: {"description": "La cita ya no está agendada o es de otro médico."},
    },
)
async def start_scheduled_consultation(
    consultation_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("queue.take")),
) -> ConsultationResponse:
    """El médico inicia su cita agendada (Agenda): la pasa a `in_progress`, le crea la sala de
    video si falta y avisa al paciente por correo ("tu médico ya está en la sala"), igual que el
    claim de la cola.

    Se puede iniciar en cualquier momento, aunque la cita sea para más tarde. El doble clic no
    duplica nada: el UPDATE es condicional sobre `status == 'scheduled'` y el segundo da 409.
    Lo clínico de la respuesta, en claro solo para el médico tratante."""
    consultation = await consultations_service.start_scheduled_consultation(
        db, consultation_id, actor_user_id=principal.id, actor_is_admin=principal.is_admin
    )
    video_args = await notifications.video_ready_mail_args(db, consultation)
    if video_args:
        background_tasks.add_task(notifications.send_video_ready_email, **video_args)
    return await _respond(
        db,
        request,
        principal,
        ConsultationResponse,
        consultation,
        _treating(principal, consultation),
    )


@router.post(
    "/{consultation_id}/derive",
    response_model=ConsultationResponse,
    summary="Derivar un caso de la cola a otra especialidad (sin tomarlo)",
    responses={
        **_NOT_FOUND,
        403: {
            "description": (
                "El caso no es de las colas del médico, o quien deriva no puede ver el motivo "
                "(p. ej. un admin que no ejerce)."
            )
        },
        409: {"description": "El caso ya no está en la cola o cambió mientras se derivaba."},
        422: {"description": "Especialidad de destino inválida, igual a la actual o sin médicos."},
    },
)
async def derive_consultation(
    consultation_id: uuid.UUID,
    payload: DeriveRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("queue.take")),
) -> ConsultationResponse:
    """Pasa un caso que nadie tomó a la cola de otra especialidad. Es el mismo caso: conserva
    su hora de llegada, así que el paciente no pierde el turno. Solo lo deriva quien puede ver
    el motivo del caso (quien lo ve en su cola y ejerce medicina; derivar sin poder leer el
    motivo no tiene sentido), y se le avisa al paciente por correo. El caso sigue sin médico,
    así que la respuesta va sin contenido clínico (`clinical_access = "none"`)."""
    consultation = await consultations_service.derive_in_queue(
        db,
        consultation_id,
        target_specialty_id=payload.specialty_id,
        actor_user_id=principal.id,
        actor_specialty_id=principal.specialty_id,
        actor_is_admin=principal.is_admin,
        principal=principal,
        ip=client_ip(request),
    )
    await _queue_derivation_email(background_tasks, db, consultation)
    return ConsultationResponse.model_validate(consultation, context=clinical_context(None))


@router.post(
    "/{consultation_id}/refer-to-queue",
    response_model=ConsultationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Derivar con especialista: cierra esta consulta y manda al paciente a otra cola",
    responses={
        **_NOT_FOUND,
        403: {"description": "Quien deriva no es el médico que atiende el caso."},
        409: {"description": "La consulta no se está atendiendo o es de otro médico."},
        422: {"description": "Especialidad de destino inválida, igual a la actual o sin médicos."},
    },
)
async def refer_to_queue(
    consultation_id: uuid.UUID,
    payload: ReferToQueueRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.close")),
) -> ConsultationResponse:
    """El médico que atiende deriva al paciente a otra especialidad, sin cita: su consulta queda
    `referred_to_specialist` (firmada, con el motivo) y se crea una consulta hija en la cola de
    la especialidad destino, con la hora de llegada original. La atiende el primer especialista
    que la tome, que verá quién la derivó y por qué. Solo deriva el médico tratante (el motivo es
    clínico). Devuelve la consulta hija, que ya no es suya: sin contenido clínico
    (`clinical_access = "none"`)."""
    child = await consultations_service.refer_to_queue(
        db,
        consultation_id,
        target_specialty_id=payload.specialty_id,
        reason=payload.reason,
        signature=payload.signature,
        actor_user_id=principal.id,
        actor_is_admin=principal.is_admin,
        actor_practices=clinical_access.practices_medicine(principal),
    )
    await _queue_derivation_email(background_tasks, db, child)
    return ConsultationResponse.model_validate(child, context=clinical_context(None))


@router.get(
    "/{consultation_id}/waiting-room",
    response_model=WaitingRoomResponse,
    summary="Estado de la sala de espera del paciente (sin sesión, con token)",
    responses={**_NOT_FOUND, **_TOKEN_RESPONSES},
)
async def waiting_room_status(
    consultation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_consultation_token),
) -> WaitingRoomResponse:
    """¿Ya hay un médico? Para `/sala-espera` y `/mi-caso`. `phase=ready` trae la sala y el
    nombre del médico; antes no hay sala que mostrar. Si el paciente fue derivado responde por el
    caso vigente de la cadena (con un token para él). Acepta el token de la consulta, la sesión
    del paciente dueño o staff. Es el respaldo del stream SSE."""
    state = await waiting_room.snapshot(db, consultation_id)
    return waiting_room.with_access_token(state, consultation_id)


@router.get(
    "/{consultation_id}/waiting-room/stream",
    summary="Estado de la sala de espera en vivo (SSE)",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "`text/event-stream`: evento `status` (mismo cuerpo que `/waiting-room`) al "
                "conectar y en cada cambio; `: ping` de latido; `gone` si el caso desaparece."
            ),
            "content": {"text/event-stream": {}},
        },
        **_NOT_FOUND,
        **_TOKEN_RESPONSES,
    },
)
async def waiting_room_stream(
    consultation_id: uuid.UUID,
    x_consultation_token: str | None = Header(default=None, alias=_CONSULTATION_TOKEN_HEADER),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_credentials),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> StreamingResponse:
    """Igual que `/waiting-room`, pero empuja los cambios: el botón "Entrar a la videoconsulta"
    aparece solo cuando un médico toma el caso, sin recargar.

    Se autoriza y se valida que el caso exista con una sesión corta ANTES de abrir el stream
    (así un 401/404 es una respuesta normal), y cada lectura del stream abre y cierra la suya:
    no se retiene una conexión del pool por paciente en espera. El stream termina a los
    `WAITING_ROOM_STREAM_MAX_SECONDS` o cuando el caso termina; el cliente reconecta."""
    async with session_factory() as db:
        principal = await get_current_principal(credentials, db) if credentials else None
        await authorize_consultation_access(db, consultation_id, x_consultation_token, principal)
        await waiting_room.current_in_chain(db, consultation_id)  # 404 antes de abrir el stream

    async def fetch() -> WaitingRoomResponse:
        async with session_factory() as session:
            return await waiting_room.snapshot(session, consultation_id)

    async def fetch_latest_message():
        async with session_factory() as session:
            return await messaging.get_latest_patient_message_signal(session, consultation_id)

    events = waiting_room.sse_events(
        fetch,
        requested_id=consultation_id,
        poll_seconds=settings.WAITING_ROOM_POLL_SECONDS,
        heartbeat_seconds=settings.WAITING_ROOM_HEARTBEAT_SECONDS,
        max_seconds=settings.WAITING_ROOM_STREAM_MAX_SECONDS,
        fetch_latest_message=fetch_latest_message,
    )
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/{consultation_id}/entered-call",
    response_model=ConsultationResponse,
    summary="Marcar que el paciente entró a la videollamada (idempotente, sin sesión)",
    responses={**_NOT_FOUND, **_TOKEN_RESPONSES},
)
@limiter.limit(settings.PUBLIC_WRITE_RATE_LIMIT)
async def mark_entered_call(
    request: Request,
    consultation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_consultation_token),
) -> ConsultationResponse:
    """Registra `entered_call_at` una sola vez, si la consulta está en `waiting`/`in_progress`.
    Reemplaza la RPC mark_patient_entered_call. Sin sesión: el paciente en la sala puede no
    estar autenticado, pero debe presentar el token de acceso de SU consulta. La respuesta va
    sin contenido clínico (`clinical_access = "none"`): el token de sala no da acceso a él."""
    consultation = await consultations_service.mark_entered_call(db, consultation_id)
    return ConsultationResponse.model_validate(consultation, context=clinical_context(None))


@router.post(
    "/{consultation_id}/video-room",
    response_model=ConsultationResponse,
    summary="Generar/obtener la sala de video (idempotente, sin sesión)",
    responses={
        **_NOT_FOUND,
        **_TOKEN_RESPONSES,
        409: {"description": "La consulta ya no está abierta."},
    },
)
@limiter.limit(settings.PUBLIC_WRITE_RATE_LIMIT)
async def ensure_video_room(
    request: Request,
    consultation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_consultation_token),
) -> ConsultationResponse:
    """Genera la sala Jitsi si no existe (mientras el caso siga en espera o en atención); si ya
    existe, devuelve la misma URL (idempotente). Exige el token de acceso de ESA consulta:
    devolver la URL de una videoconsulta médica a quien solo conozca el id era el hallazgo M3.
    La respuesta va sin contenido clínico (`clinical_access = "none"`)."""
    consultation = await consultations_service.ensure_video_room(db, consultation_id)
    return ConsultationResponse.model_validate(consultation, context=clinical_context(None))


# --- Eventos / auditoría de la consulta ---


@router.get(
    "/{consultation_id}/events",
    response_model=list[ConsultationEventResponse],
    summary="Listar eventos de la consulta (staff)",
    responses={
        **_NOT_FOUND,
        403: {"description": "Solo el médico que atiende el caso o el equipo admin."},
    },
)
async def list_consultation_events(
    consultation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.read")),
) -> list[ConsultationEventResponse]:
    """Historial de auditoría de la consulta (cronológico). Las notas de los eventos son del
    médico: en claro para el médico tratante (`full`); el admin recibe el historial con `note`
    en null; cualquier otro médico, 403 (auditado)."""
    consultation = await consultations_service.get_consultation(db, consultation_id)  # 404
    grant = await _treating_or_admin(db, request, principal, consultation)
    events = await consultations_service.list_events(db, consultation_id)
    items = [
        ConsultationEventResponse.model_validate(e, context=clinical_context(grant))
        for e in events
    ]
    await _audit_read(db, request, principal, [(consultation_id, grant)])
    return items


@router.post(
    "/{consultation_id}/events",
    response_model=ConsultationEventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar evento de la consulta (staff)",
    responses={
        **_NOT_FOUND,
        400: {"description": "El `consultation_id` del cuerpo no coincide con la ruta."},
        403: {"description": "Evento con `note` de quien no es el médico que atiende el caso."},
        409: {"description": "La consulta está asignada a otro médico."},
    },
)
async def create_consultation_event(
    consultation_id: uuid.UUID,
    payload: ConsultationEventCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.write")),
) -> ConsultationEventResponse:
    """Registra un evento en el historial del caso (autor = el JWT). Un evento con `note` solo lo
    escribe el médico tratante (la nota es clínica); el admin registra eventos sin nota."""
    event = await consultations_service.create_event(
        db,
        consultation_id,
        payload,
        created_by=principal.id,
        actor_is_admin=principal.is_admin,
        actor_practices=clinical_access.practices_medicine(principal),
    )
    consultation = await consultations_service.get_consultation(db, consultation_id)
    grant = _treating(principal, consultation)
    out = ConsultationEventResponse.model_validate(event, context=clinical_context(grant))
    await _audit_read(db, request, principal, [(consultation_id, grant)])
    return out
