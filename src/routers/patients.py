"""Capa HTTP (delgada) para patients. La lógica vive en src.services.patients.

Autorización: crear es público (alta del paciente); leer requiere staff; editar y
eliminar requieren admin (replica las RLS).

`description` y `allergies` son clínicos (cifrados): los lee el propio paciente (`/me`) y el
médico habilitado que lo trata; el admin recibe la ficha con esos campos en null. Cada lectura
concedida queda en `audit_log` (`READ_CLINICAL_DATA`, resource `patients`).
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.errors import ForbiddenError
from src.core.observability import client_ip
from src.core.ratelimit import limiter
from src.core.security import Principal, get_current_principal, require_permission
from src.db.session import get_db
from src.models.patient import Patient
from src.schemas.clinical import ClinicalGrant, clinical_context
from src.schemas.patient import (
    PatientAddressResponse,
    PatientCreate,
    PatientResponse,
    PatientUpdate,
)
from src.services import clinical_access, email_verification
from src.services import patients as patients_service

router = APIRouter(prefix="/patients", tags=["patients"])
tag_metadata = [
    {"name": "patients", "description": "Pacientes (alta con consentimiento, consulta, edición)."}
]

_NOT_FOUND = {404: {"description": "Paciente no encontrado."}}


def _patient_response(
    patient: Patient, principal: Principal, grant: ClinicalGrant | None
) -> PatientResponse:
    """Serializa el paciente con el teléfono de emergencia solo para quien corresponde, y los
    campos clínicos solo con `grant`.

    El teléfono es PII de contacto pedida para emergencias: la ven el equipo admin, el médico
    dueño del paciente de consultorio y el propio paciente. El médico tratante la recibe en el
    detalle de su consulta (GET /consultations/{id}), no por acá."""
    response = PatientResponse.model_validate(patient, context=clinical_context(grant))
    may_see = (
        principal.is_admin
        or patient.created_by_doctor_id == principal.id
        or patient.user_id == principal.id
    )
    if not may_see:
        response.emergency_phone = None
    return response


async def _staff_responses(
    db: AsyncSession, request: Request, principal: Principal, patients: list[Patient]
) -> list[PatientResponse]:
    """Respuesta de staff: clínico solo para el médico que trata a cada paciente, y la lectura
    concedida queda auditada (una fila por llamada, con los ids)."""
    grants = await patients_service.clinical_grants(db, principal, patients)
    responses = [_patient_response(p, principal, grants[p.id]) for p in patients]
    await clinical_access.audit_clinical_read(
        db, principal=principal, ip=client_ip(request), resource="patients", grants=grants.items()
    )
    return responses


@router.get(
    "",
    response_model=list[PatientResponse],
    summary="Listar pacientes (staff)",
    responses={403: {"description": "`scope=all` requiere el permiso patients.write."}},
)
async def list_patients(
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    scope: Literal["public", "all"] = Query(
        "public",
        description=(
            "'public' (default) = solo pacientes de la cola pública. 'all' incluye además los "
            "de consultorio registrados por médicos; requiere patients.write."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("patients.read")),
) -> list[PatientResponse]:
    """Lista paginada de pacientes (más recientes primero).

    Por defecto excluye los pacientes **de consultorio**: son privados del médico que los
    registró y `patients.read` lo tiene todo médico. Para verlos hace falta `patients.write`
    (admin) y pedir `scope=all` explícitamente.

    `description`/`allergies` solo salen para el médico que trata a ese paciente (su paciente de
    consultorio, o con una consulta suya asignada); al admin y a otros médicos, en null con
    `clinical_access = "none"`."""
    if scope == "all" and not principal.has_permission("patients.write"):
        raise ForbiddenError(
            "Ver los pacientes de consultorio requiere el permiso patients.write."
        )
    patients = await patients_service.list_patients(
        db, skip=skip, limit=limit, include_doctor_patients=scope == "all"
    )
    return await _staff_responses(db, request, principal, patients)


@router.post(
    "",
    response_model=PatientResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear paciente (público)",
    responses={
        400: {"description": "Falta el consentimiento (`consent = true`)."},
        403: {"description": "Verifica tu correo antes de continuar."},
        429: {"description": "Demasiadas altas desde esta IP (rate limit)."},
    },
)
@limiter.limit(settings.PUBLIC_WRITE_RATE_LIMIT)
async def create_patient(
    request: Request, payload: PatientCreate, db: AsyncSession = Depends(get_db)
) -> PatientResponse:
    """Crea un paciente. Requiere `consent = true` (igual que la política RLS pública).

    `request` es obligatorio para slowapi (lee la IP del cliente), aunque no se use aquí.

    Verificación de correo: si `EMAIL_VERIFICATION_REQUIRED` y el payload trae email,
    exige `email_verification_token` válido para ese correo y propósito "patient".
    Sin email (alta anónima por API) no se exige nada.

    La respuesta lleva `description`/`allergies` en null: quien llama es anónimo y el alta no
    le concede leer datos clínicos (se guardan cifrados). El paciente los ve en `/patients/me`."""
    email_verification.ensure_verified(
        token=payload.email_verification_token, email=payload.email, purpose="patient"
    )
    patient = await patients_service.create_patient(db, payload)
    return PatientResponse.model_validate(patient)


@router.get(
    "/me",
    response_model=list[PatientResponse],
    summary="Mis registros de paciente (portal del paciente)",
)
async def list_my_patients(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> list[PatientResponse]:
    """Registros de paciente ligados a la cuenta del llamante (mi-caso). Replica la RLS
    patients_select_own (user_id = auth.uid()); no requiere el permiso staff patients.read.
    Debe ir ANTES de /{patient_id} o FastAPI intenta parsear 'me' como UUID (422).

    Incluye su propia descripción y alergias (`clinical_access = "summary"`); la lectura queda
    auditada como la de cualquier otro."""
    patients = await patients_service.list_patients_for_user(db, principal.id)
    grant = clinical_access.patient_owner_grant()
    responses = [_patient_response(patient, principal, grant) for patient in patients]
    await clinical_access.audit_clinical_read(
        db,
        principal=principal,
        ip=client_ip(request),
        resource="patients",
        grants=[(p.id, grant) for p in patients],
    )
    return responses


@router.get(
    "/{patient_id}",
    response_model=PatientResponse,
    summary="Obtener paciente (staff)",
    responses={
        **_NOT_FOUND,
        403: {"description": "Paciente de consultorio sin `patients.write` (queda auditado)."},
    },
)
async def get_patient(
    patient_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("patients.read")),
) -> PatientResponse:
    """Un paciente de la cola pública. Los de consultorio dan 403 por acá: los lee su médico en
    `/doctors/me/patients/{id}`, o un admin (`patients.write`).

    `description`/`allergies` solo para el médico que lo trata (ver `GET /patients`). El 403
    queda en `audit_log` como intento denegado."""
    patient = await patients_service.get_patient_as_staff(
        db,
        patient_id,
        principal=principal,
        may_see_doctor_patients=principal.has_permission("patients.write"),
        ip=client_ip(request),
    )
    return (await _staff_responses(db, request, principal, [patient]))[0]


@router.patch(
    "/{patient_id}",
    response_model=PatientResponse,
    summary="Actualizar paciente (admin)",
    responses={
        **_NOT_FOUND,
        403: {
            "description": "`description`/`allergies` enviados por quien no es el médico "
            "habilitado que trata al paciente."
        },
    },
)
async def update_patient(
    patient_id: uuid.UUID,
    payload: PatientUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("patients.write")),
) -> PatientResponse:
    """Edita la ficha. `description`/`allergies` son historia clínica: solo los escribe el
    médico habilitado que trata al paciente (su paciente de consultorio o con una consulta suya
    asignada); si otro (el admin incluido) los manda, 403. El resto de la ficha lo edita el
    admin, y la respuesta trae lo clínico en null a quien no trata al paciente."""
    patient = await patients_service.update_patient(db, patient_id, payload, principal=principal)
    return (await _staff_responses(db, request, principal, [patient]))[0]


@router.delete(
    "/{patient_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archivar paciente (baja lógica, admin)",
    responses=_NOT_FOUND,
)
async def delete_patient(
    patient_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    # Baja lógica (soft delete), no hard delete: se gatea con patients.write, igual que doctors.
    principal: Principal = Depends(require_permission("patients.write")),
) -> None:
    await patients_service.delete_patient(db, patient_id, actor_user_id=principal.id)


@router.get(
    "/{patient_id}/address",
    response_model=PatientAddressResponse,
    summary="Dirección cifrada del paciente (solo tratante o allowlist)",
    responses={
        403: {
            "description": (
                "Solo el médico que atiende el caso (o allowlist) puede ver la dirección."
            )
        },
        404: {"description": "Paciente no encontrado."},
    },
)
async def get_patient_address(
    patient_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("patients.read")),
) -> PatientAddressResponse:
    """Devuelve la dirección cifrada E2E (v1:base64 sealed box) del paciente.

    Autorizado solo si:
    - El email del principal está en la allowlist configurable (`ADDRESS_VIEWER_EMAILS`), O
    - El principal es el médico asignado a alguna consulta de este paciente (cualquier estado).

    Escribe auditoría con action="patient.address_revealed" antes de responder.
    El servidor NUNCA descifra, loguea ni valida el contenido.
    """
    patient = await patients_service.get_patient_as_staff(
        db,
        patient_id,
        principal=principal,
        may_see_doctor_patients=principal.has_permission("patients.write"),
        ip=client_ip(request),
    )
    address_encrypted = await patients_service.patient_address_for_viewer(
        db, patient, viewer_id=principal.id, viewer_email=principal.email
    )
    return PatientAddressResponse(address_encrypted=address_encrypted)
