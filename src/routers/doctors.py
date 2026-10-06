"""Capa HTTP (delgada) para doctors. La lógica vive en src.services.doctors.

Autorización:
- Registrar (`POST`): público (auto-registro). El backend verifica la credencial
  contra SACS/FPV y fija `verified`; ese es el control real de acceso: mientras la
  ficha no esté verificada y completa (cédula + licencia), el médico conserva su rol
  pero se queda SIN permisos y no puede atender (ver `has_valid_credential`).
- Leer: staff. Editar/eliminar: admin.
- **Aprobación manual** (cuando el SACS/FPV no valida al médico): `POST /{id}/approve`
  y su reverso `POST /{id}/revoke-approval`, con el permiso `doctors.verify`. Tienen
  endpoint propio para que dejen su propia entrada en `audit_log`.
- `/me` (ver y completar la ficha propia) queda fuera del gate a propósito: es como el
  médico no verificado sale del limbo.
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.ratelimit import limiter
from src.core.security import Principal, get_current_principal, require_permission
from src.db.session import get_db
from src.schemas.doctor import (
    DoctorAdminItem,
    DoctorAdminPage,
    DoctorBlockedReason,
    DoctorContactResponse,
    DoctorCreate,
    DoctorCredentialSummary,
    DoctorMeResponse,
    DoctorPoolPage,
    DoctorRegistrationCheckRequest,
    DoctorRegistrationCheckResponse,
    DoctorResponse,
    DoctorSelfUpdate,
    DoctorUpdate,
    SpecialtyRequestItem,
    SpecialtyRequestPage,
    SpecialtyRequestResolve,
)
from src.services import doctors as doctors_service
from src.services import email_verification, registration_mail

router = APIRouter(prefix="/doctors", tags=["doctors"])
tag_metadata = [
    {"name": "doctors", "description": "Médicos y psicólogos: registro con verificación SACS/FPV."}
]

_NOT_FOUND = {404: {"description": "Médico no encontrado."}}


async def _queue_registration_mail(
    background: BackgroundTasks, db: AsyncSession, doctor, reason: str | None
) -> None:
    """Encola los DOS correos de un registro de médico: el aviso a operación y el que recibe
    el propio médico.

    Los args se resuelven aquí, con la sesión viva (el BackgroundTask corre tras cerrar la
    request). Salen en pareja: si quedó verificado, aviso interno + "ya puedes entrar"; si no,
    aviso interno con el motivo + la petición de documentos.

    El correo AL MÉDICO no depende de `MAIL_INTERNAL_RECIPIENTS`: aunque operación no tenga
    buzón configurado, quien se registró merece su respuesta.
    """
    internal = await registration_mail.doctor_registered_mail_args(db, doctor)
    if internal:
        background.add_task(
            registration_mail.send_doctor_registered_alert, reason=reason, **internal
        )
    if doctor.verified:
        approved = await registration_mail.doctor_approved_mail_args(db, doctor)
        if approved:
            background.add_task(registration_mail.send_doctor_approved_email, **approved)
        return
    # Mismo resolutor de destinatario que la rama de arriba, a propósito: son la misma pregunta
    # ("¿a dónde le escribo a este médico?") y responderla de dos formas distintas hacía que un
    # médico sin email en la ficha recibiera un correo pero no el otro.
    to_email = await registration_mail.doctor_email(db, doctor)
    if to_email:
        background.add_task(
            registration_mail.send_doctor_rejected_email,
            to_email=to_email,
            full_name=doctor.full_name,
            cedula=doctor.cedula,
            reason=reason,
        )


@router.get(
    "",
    response_model=DoctorAdminPage,
    summary="Listar médicos con su estado de habilitación (staff, paginado)",
    responses={403: {"description": "Requiere el permiso doctors.read."}},
)
async def list_doctors(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    status_filter: int | None = Query(
        None, alias="status", ge=0, le=2, description="0=de baja · 1=activo · 2=expulsado."
    ),
    verified: bool | None = Query(
        None, description="true=credencial aprobada · false=no aprobada · omitir=ambas."
    ),
    can_practice: bool | None = Query(
        None,
        description="true=habilitados para atender · false=bloqueados · omitir=todos. "
        "NO es lo mismo que `verified`: exige además ficha activa, cédula y licencia.",
    ),
    blocked_reason: DoctorBlockedReason | None = Query(
        None,
        description="Motivo de bloqueo exacto. `no_verificado` = los que un admin puede "
        "aprobar ahora mismo; `sin_cedula`/`sin_licencia` = hay que pedirles el dato.",
    ),
    search: str | None = Query(None, description="Filtra por nombre, cédula o email."),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("doctors.read")),
) -> DoctorAdminPage:
    """Médicos para la tabla del panel admin: página + `total` exacto.

    Incluye las fichas de `doctors` y también las cuentas con rol de médico que aún no
    crearon ficha (`id: null`), que si no serían invisibles.

    Cada fila trae `can_practice` (el criterio real de acceso) y `blocked_reason`
    (`sin_ficha` · `de_baja` · `sin_cedula` · `sin_licencia` · `no_verificado`). Solo
    `no_verificado` se resuelve con `POST /doctors/{id}/approve`; el resto necesita que el
    médico complete su ficha.

    Para la cola de trabajo del admin, filtra por `blocked_reason=no_verificado`: sin eso
    los aprobables quedan diluidos entre miles de fichas sin cédula.
    """
    items, total = await doctors_service.list_doctors(
        db,
        skip=skip,
        limit=limit,
        status=status_filter,
        verified=verified,
        can_practice=can_practice,
        blocked_reason=blocked_reason,
        search=search,
    )
    return DoctorAdminPage(items=[DoctorAdminItem(**item) for item in items], total=total)


@router.post(
    "",
    response_model=DoctorResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar médico (público; verifica en SACS/FPV)",
    responses={
        403: {"description": "Verifica tu correo antes de continuar."},
        422: {"description": "Formato de cédula/teléfono inválido."},
        429: {"description": "Demasiados registros desde esta IP (rate limit)."},
    },
)
@limiter.limit(settings.DOCTOR_REGISTER_RATE_LIMIT)
async def register_doctor(
    request: Request,
    payload: DoctorCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> DoctorResponse:
    """Registra un médico/psicólogo. El backend valida la cédula contra el SACS
    (médico) o la FPV (psicólogo) según el `professional_type`.

    `verified` queda en `true` **solo** si ese registro encuentra la cédula y devuelve
    nombre y licencia; entonces el `full_name` y el `license` de la ficha son los del
    registro, no los del payload (lo que declara el cliente no verifica nada). En
    cualquier otro caso queda `false` y lo aprueba un admin (`POST /{id}/approve`).

    Anti-bot: rate limit por IP + campo honeypot (`website`, debe ir vacío).

    Verificación de correo: si `EMAIL_VERIFICATION_REQUIRED` y el payload trae email,
    exige `email_verification_token` válido para ese correo y propósito "doctor".

    Manda dos correos (best-effort, en background): uno a operación con el expediente y el
    veredicto, y otro al médico — de bienvenida si quedó verificado, o pidiéndole título,
    licencia del SACS y carta de artículo 8 si no."""
    email_verification.ensure_verified(
        token=payload.email_verification_token, email=payload.email, purpose="doctor"
    )
    doctor, reason = await doctors_service.create_doctor(db, payload)
    await _queue_registration_mail(background_tasks, db, doctor, reason)
    return doctor


@router.post(
    "/registration-check",
    response_model=DoctorRegistrationCheckResponse,
    summary="Chequeo previo al registro: ¿el correo o la cédula ya están registrados? (público)",
    responses={
        422: {"description": "Correo o cédula con formato inválido."},
        429: {"description": "Demasiadas consultas desde esta IP (rate limit)."},
    },
)
@limiter.limit(settings.REGISTRATION_CHECK_RATE_LIMIT)
async def registration_check(
    request: Request,
    payload: DoctorRegistrationCheckRequest,
    db: AsyncSession = Depends(get_db),
) -> DoctorRegistrationCheckResponse:
    """Lo consulta `/registro-medico` ANTES de crear la cuenta en Supabase Auth: al salir del
    campo de correo (para avisar "ya estás registrado como médico") y otra vez al enviar, con la
    cédula.

    Existe para cortar las cuentas huérfanas: el alta crea la cuenta de Auth y luego la ficha, y
    si la ficha choca con un correo o una cédula ya registrados, la cuenta queda sin ficha.

    **Sobre la enumeración:** dice si un correo tiene cuenta. No abre un oráculo nuevo —el
    `signUp` de Supabase ya responde "User already registered" para ese mismo correo—, pero por
    eso va con rate limit por IP. Solo lectura."""
    status_, cedula_taken = await doctors_service.check_registration(
        db, payload.email, payload.cedula
    )
    return DoctorRegistrationCheckResponse(email_status=status_, cedula_taken=cedula_taken)


# --- Perfil propio del médico autenticado (self-service) ---
# Declarados ANTES de "/{doctor_id}" para que "me" no se interprete como UUID.


@router.get(
    "/me",
    response_model=DoctorMeResponse,
    summary="Ver mi perfil de médico",
    responses={404: {"description": "No tienes un perfil de médico."}},
)
async def get_my_doctor(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> DoctorMeResponse:
    """Perfil del médico autenticado (identidad tomada del JWT). Devuelve la fila en
    `doctors`; si no existe (médicos que entraron por Google/`finalize-role`), cae a
    la cuenta en `users`. IDOR-safe: el recurso sale del token, nunca de la URL."""
    return await doctors_service.get_my_profile(db, principal.id)


@router.patch(
    "/me",
    response_model=DoctorMeResponse,
    summary="Actualizar mi perfil de médico",
    responses={
        404: {"description": "No tienes un perfil de médico."},
        409: {"description": "La cédula ya pertenece a otro médico."},
        422: {
            "description": (
                "Datos inválidos, campos no permitidos (status/verified/email/phone) o "
                "falta `professional_type_id` para verificar la cédula (cuenta sin ficha)."
            )
        },
    },
)
async def update_my_doctor(
    payload: DoctorSelfUpdate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> DoctorMeResponse:
    """Auto-edición de nombre, licencia, especialidad y cédula.

    - Con ficha (`source:"doctor"`): cambiar la cédula re-verifica contra SACS/FPV y
      recalcula `verified`. No permite tocar `status`/`verified`/`email`/`phone` ni el
      tipo profesional.
    - Sin ficha (`source:"user"`, médico de Google): enviar `cedula` + `professional_type_id`
      verifica la credencial y **crea** la ficha en `doctors`, promoviendo la cuenta a
      `source:"doctor"` (`verified` según SACS/FPV).

    Si la verificación sale bien, el `full_name` y el `license` que quedan son los del
    registro oficial, aunque este mismo PATCH mande otros."""
    return await doctors_service.update_my_profile(db, principal.id, payload)


# NOTA: debe ir ANTES de "/{doctor_id}" o FastAPI intenta parsear el literal como UUID (422).
@router.get(
    "/specialty-requests",
    response_model=SpecialtyRequestPage,
    summary="Especialidades escritas a mano por médicos, pendientes de revisión (admin)",
    responses={403: {"description": "Requiere el permiso doctors.verify."}},
)
async def specialty_requests(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("doctors.verify")),
) -> SpecialtyRequestPage:
    """Médicos cuya especialidad no estaba en el catálogo y la escribieron en su perfil. No ven
    la cola hasta que un admin la agregue al catálogo (o les asigne una existente) con
    `POST /doctors/{id}/specialty-request/resolve`. Las más antiguas primero."""
    rows, total = await doctors_service.list_specialty_requests(db, skip=skip, limit=limit)
    return SpecialtyRequestPage(
        items=[SpecialtyRequestItem.model_validate(r, from_attributes=True) for r in rows],
        total=total,
    )


@router.get(
    "/credential-summary",
    response_model=DoctorCredentialSummary,
    summary="Resumen de credenciales: cuántos atienden y cuántos están bloqueados (staff)",
    responses={403: {"description": "Requiere el permiso doctors.read."}},
)
async def doctor_credential_summary(
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("doctors.read")),
) -> DoctorCredentialSummary:
    """Contadores por estado de credencial, para la cabecera del panel admin.

    Responde de un vistazo "¿qué tengo que hacer hoy?": `no_verificado` es la cola de
    aprobación del admin, mientras que `sin_cedula`/`sin_licencia` solo se resuelven
    pidiéndole los datos al médico. Cada contador se corresponde con un valor de
    `blocked_reason`, así que sirve de atajo a `GET /doctors?blocked_reason=...`."""
    return DoctorCredentialSummary(**await doctors_service.credential_summary(db))


# NOTA: debe ir ANTES de "/{doctor_id}" o FastAPI intenta parsear "pool" como UUID (422).
@router.get(
    "/pool",
    response_model=DoctorPoolPage,
    summary="Pool de médicos para referir/agendar (paginado, con estado online)",
    responses={
        401: {"description": "Sin token o token inválido."},
        403: {"description": "Requiere el permiso doctors.read."},
    },
)
async def doctor_pool(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    specialty_id: uuid.UUID | None = Query(None),
    professional_type_id: uuid.UUID | None = Query(None),
    search: str | None = Query(None, description="Filtra por nombre (ILIKE)."),
    online: bool | None = Query(
        None, description="true=solo online · false=solo offline · omitir=todos"
    ),
    online_ids: list[uuid.UUID] | None = Query(
        None, description="user_ids que el cliente sabe online por Presence (para filtrar online)."
    ),
    exclude_self: bool = Query(
        True,
        description="true (default): oculta al médico que consulta (referir no se aplica a uno "
        "mismo). false: lo incluye — el dashboard admin lo usa para que el conteo y la lista "
        "cuadren cuando el propio admin es médico y está online.",
    ),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("doctors.read")),
) -> DoctorPoolPage:
    """Médicos activos (status=1) para referir/agendar durante una consulta. Filtrable por nombre
    (`search`), especialidad y tipo. El estado online lo resuelve el frontend con Realtime Presence
    y lo pasa como `online_ids` + `online` (true/false) para filtrar sin romper la paginación. NO
    trae el teléfono (se revela con POST .../contact). Excluye al médico que consulta salvo que
    `exclude_self=false` (dashboard admin)."""
    items, total = await doctors_service.list_doctor_pool(
        db,
        skip=skip,
        limit=limit,
        specialty_id=specialty_id,
        professional_type_id=professional_type_id,
        search=search,
        online=online,
        online_user_ids=online_ids,
        exclude_user_id=principal.id if exclude_self else None,
    )
    return DoctorPoolPage(items=items, total=total)


@router.post(
    "/{doctor_id}/contact",
    response_model=DoctorContactResponse,
    summary="Revelar el WhatsApp de un médico del pool (queda auditado)",
    responses=_NOT_FOUND,
)
async def reveal_doctor_contact(
    doctor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("doctors.read")),
) -> DoctorContactResponse:
    """Devuelve el teléfono de contacto del médico y REGISTRA en audit_log que este usuario lo vio
    (para la bitácora del panel admin). El número no aparece en el listado del pool: solo aquí."""
    phone = await doctors_service.reveal_doctor_contact(db, doctor_id, viewer_user_id=principal.id)
    return DoctorContactResponse(phone=phone)


@router.get(
    "/{doctor_id}",
    response_model=DoctorResponse,
    summary="Obtener médico (staff)",
    responses=_NOT_FOUND,
)
async def get_doctor(
    doctor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("doctors.read")),
) -> DoctorResponse:
    return await doctors_service.get_doctor(db, doctor_id)


@router.patch(
    "/{doctor_id}",
    response_model=DoctorResponse,
    summary="Actualizar médico (admin)",
    responses=_NOT_FOUND,
)
async def update_doctor(
    doctor_id: uuid.UUID,
    payload: DoctorUpdate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("doctors.write")),
) -> DoctorResponse:
    """Edición administrativa de la ficha de un médico (queda como `doctor.updated`).

    La aprobación de la credencial **no** va por aquí: es `POST /doctors/{id}/approve`,
    para que tenga su propia entrada en `audit_log`."""
    return await doctors_service.update_doctor(db, doctor_id, payload, actor_user_id=principal.id)


@router.post(
    "/{doctor_id}/approve",
    response_model=DoctorResponse,
    summary="Aprobar la credencial de un médico (admin)",
    responses={
        **_NOT_FOUND,
        403: {"description": "Requiere el permiso doctors.verify."},
        422: {"description": "La ficha no tiene cédula/licencia, o está de baja o expulsada."},
    },
)
async def approve_doctor(
    doctor_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("doctors.verify")),
) -> DoctorResponse:
    """Habilita para atender a un médico al que el SACS/FPV no validó.

    Es la vía de escape del gate de credencial cuando el registro oficial no responde o no
    tiene al profesional. Queda en `audit_log` como `doctor.approved` con el admin que
    aprobó — distinta del `doctor.updated` genérico porque es la traza de que un humano
    dejó atender a alguien que el registro no respaldó.

    **422 si la ficha no tiene cédula y licencia, o no está activa:** aprobarla no
    habilitaría a nadie (el gate la seguiría bloqueando) y dejaría al admin creyendo que
    sí. El mensaje dice qué falta. Idempotente.

    Al aprobar se le avisa al médico por correo (best-effort). **Solo cuando la llamada cambia
    el estado**: aprobar una ficha ya aprobada sigue devolviendo 200, pero no vuelve a
    escribirle — un segundo clic del admin no debe producir un segundo "ya puedes entrar".
    A operación no se le avisa: quien aprobó estaba mirando la pantalla cuando lo hizo."""
    doctor, newly_approved = await doctors_service.approve_doctor(
        db, doctor_id, actor_user_id=principal.id
    )
    if newly_approved:
        args = await registration_mail.doctor_approved_mail_args(db, doctor)
        if args:
            background_tasks.add_task(registration_mail.send_doctor_approved_email, **args)
    return doctor


@router.post(
    "/{doctor_id}/specialty-request/resolve",
    response_model=DoctorResponse,
    summary="Asignar la especialidad que un médico pidió agregar (admin)",
    responses={
        **_NOT_FOUND,
        403: {"description": "Requiere el permiso doctors.verify."},
        409: {"description": "El médico no tiene una especialidad pendiente de revisión."},
        422: {"description": "La especialidad no existe, está borrada o es de relleno."},
    },
)
async def resolve_specialty_request(
    doctor_id: uuid.UUID,
    payload: SpecialtyRequestResolve,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("doctors.verify")),
) -> DoctorResponse:
    """Le asigna al médico una especialidad del catálogo y cierra su solicitud: desde ese
    momento ve la cola de esa especialidad. Si la que escribió no existía, se crea primero con
    `POST /specialties`. Queda en `audit_log` como `doctor.specialty_request_resolved`."""
    return await doctors_service.resolve_specialty_request(
        db, doctor_id, payload.specialty_id, actor_user_id=principal.id
    )


@router.post(
    "/{doctor_id}/revoke-approval",
    response_model=DoctorResponse,
    summary="Revocar la aprobación de un médico (admin)",
    responses={**_NOT_FOUND, 403: {"description": "Requiere el permiso doctors.verify."}},
)
async def revoke_doctor_approval(
    doctor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("doctors.verify")),
) -> DoctorResponse:
    """Deshace la aprobación (`verified` vuelve a false) y el médico deja de atender.

    Para una aprobación por error o una credencial impugnada. Queda en `audit_log` como
    `doctor.approval_revoked`. Idempotente."""
    return await doctors_service.revoke_doctor_approval(db, doctor_id, actor_user_id=principal.id)


@router.delete(
    "/{doctor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar médico (admin, baja lógica)",
    responses=_NOT_FOUND,
)
async def delete_doctor(
    doctor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("doctors.write")),
) -> None:
    await doctors_service.delete_doctor(db, doctor_id, actor_user_id=principal.id)
