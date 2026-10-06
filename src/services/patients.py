"""Capa de negocio para patients."""

import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.errors import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    UnprocessableError,
)
from src.models.consultation import Consultation
from src.models.patient import Patient
from src.schemas.clinical import ClinicalGrant, treating_grant
from src.schemas.patient import (
    DoctorPatientCreate,
    DoctorPatientUpdate,
    PatientCreate,
    PatientUpdate,
)
from src.services import audit

if TYPE_CHECKING:  # security -> services/__init__ -> patients: import circular en runtime
    from src.core.security import Principal

# Campos clínicos de la ficha (cifrados): los escribe solo el médico que trata al paciente.
_CLINICAL_UPDATE_FIELDS = frozenset({"description", "allergies"})


async def _deny(
    session: AsyncSession,
    principal: "Principal",
    ip: str | None,
    patient_id: uuid.UUID,
    message: str,
) -> NoReturn:
    """403 sobre UN paciente concreto, con su traza `READ_CLINICAL_DATA` (outcome "denied")
    commiteada antes de lanzar: si no, el rollback del error se la llevaría."""
    from src.services import clinical_access  # diferido: mismo ciclo que `Principal`

    await clinical_access.audit_clinical_denied(
        session, principal=principal, ip=ip, resource="patients", resource_id=patient_id
    )
    raise ForbiddenError(message)


def _ensure_emergency_phone_differs(emergency: str | None, whatsapp: str | None) -> None:
    """El teléfono de emergencia debe ser distinto del WhatsApp.

    El esquema Pydantic lo valida cuando ambos viajan en el mismo payload; acá se cubre el
    PATCH (donde puede venir solo uno) y el alta por médico, comparando contra lo ya guardado.
    """
    if emergency is None:
        return
    if re.sub(r"\D", "", emergency) == re.sub(r"\D", "", whatsapp or ""):
        raise UnprocessableError("El teléfono de emergencia debe ser distinto al de WhatsApp.")


async def _resolve_dependent_cedula(session: AsyncSession, parent_id: uuid.UUID) -> str | None:
    """Cédula sintética para un menor sin cédula propia: cédula del adulto responsable
    + correlativo de carga familiar (1, 2, 3...). P. ej. adulto 24319284 -> primer
    menor 243192841, segundo menor 243192842. Sin cédula en el adulto, no hay base
    para generarla (queda None, no es un error)."""
    guardian = await session.get(Patient, parent_id)
    if guardian is None or not guardian.cedula:
        return None
    dependientes = await session.scalar(
        select(func.count()).select_from(Patient).where(Patient.parent_id == parent_id)
    )
    return f"{guardian.cedula}{(dependientes or 0) + 1}"


async def list_patients(
    session: AsyncSession,
    skip: int = 0,
    limit: int = 100,
    include_doctor_patients: bool = False,
) -> list[Patient]:
    """Listado staff. Por defecto **solo pacientes de la cola pública**.

    Los de consultorio (`created_by_doctor_id` no nulo) quedan fuera: nunca entraron a la
    plataforma, y `patients.read` lo tiene TODO médico — sin este filtro, cualquier colega leería
    el nombre, la cédula y las alergias de los pacientes privados de otro. Sería incoherente
    anonimizar el caso en la bandeja de interconsultas y regalar la ficha completa por acá.
    `include_doctor_patients` es para quien tiene `patients.write` (admin), que sí necesita la
    visión completa para operar.
    """
    stmt = select(Patient).where(Patient.deleted_at.is_(None))  # soft delete: no listar archivados
    if not include_doctor_patients:
        stmt = stmt.where(Patient.created_by_doctor_id.is_(None))
    stmt = stmt.order_by(Patient.created_at.desc(), Patient.id).offset(skip).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_patient_as_staff(
    session: AsyncSession,
    patient_id: uuid.UUID,
    *,
    principal: "Principal",
    may_see_doctor_patients: bool,
    ip: str | None = None,
) -> Patient:
    """Lectura staff de un paciente. Misma frontera que `list_patients`, aplicada al detalle:
    un guard que cubre el listado pero no el `GET /{id}` no es un guard (lección del sub-recurso
    `/events` en @.claude/rules/security.md). El 403 queda auditado como intento denegado."""
    patient = await get_patient(session, patient_id)
    if patient.created_by_doctor_id is not None and not may_see_doctor_patients:
        await _deny(
            session,
            principal,
            ip,
            patient.id,
            "Este paciente es de consultorio: solo lo ve el médico que lo registró.",
        )
    return patient


async def list_patients_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[Patient]:
    """Registros de paciente ligados a la cuenta del usuario (user_id == caller, no archivados).
    Para el portal del paciente (mi-caso), que no tiene el permiso staff patients.read; replica la
    RLS patients_select_own (user_id = auth.uid())."""
    stmt = (
        select(Patient)
        .where(Patient.user_id == user_id, Patient.deleted_at.is_(None))
        .order_by(Patient.created_at.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def clinical_grants(
    session: AsyncSession, principal: "Principal", patients: list[Patient]
) -> dict[uuid.UUID, ClinicalGrant | None]:
    """Quién lee `description`/`allergies` de cada paciente: el médico habilitado que lo trata.

    "Lo trata" = es su paciente de consultorio (`created_by_doctor_id`) o tiene alguna consulta
    de ese paciente asignada (mismo criterio que la dirección, `patient_address_for_viewer`).
    Ser admin no concede nada; el paciente dueño va por `/patients/me` con su propio permiso.
    Una sola query para todo el listado, no una por fila."""
    from src.services import clinical_access  # diferido: mismo ciclo que `Principal`

    grants: dict[uuid.UUID, ClinicalGrant | None] = {p.id: None for p in patients}
    if not patients or not clinical_access.practices_medicine(principal):
        return grants
    asignados = set(
        await session.scalars(
            select(Consultation.patient_id)
            .where(
                Consultation.patient_id.in_(list(grants)),
                Consultation.assigned_doctor_id == principal.id,
            )
            .distinct()
        )
    )
    for p in patients:
        if p.created_by_doctor_id == principal.id or p.id in asignados:
            grants[p.id] = treating_grant("assigned_doctor")
    return grants


async def get_patient(session: AsyncSession, patient_id: uuid.UUID) -> Patient:
    patient = await session.get(Patient, patient_id)
    if patient is None or patient.deleted_at is not None:  # soft delete: el archivado es 404
        raise NotFoundError("Paciente no encontrado.")
    return patient


async def create_patient(session: AsyncSession, data: PatientCreate) -> Patient:
    if not data.consent:
        raise BadRequestError("Se requiere el consentimiento del paciente (consent = true).")
    if data.parent_id is not None and await session.get(Patient, data.parent_id) is None:
        raise BadRequestError("El adulto responsable referenciado (parent_id) no existe.")
    # email_verification_token es solo para verificación previa; no se guarda en la BD.
    patient_data = data.model_dump(exclude={"email_verification_token"})
    patient = Patient(**patient_data)
    if patient.parent_id is not None and not patient.cedula:
        patient.cedula = await _resolve_dependent_cedula(session, patient.parent_id)
    if patient.consent and patient.consent_at is None:
        patient.consent_at = datetime.now(UTC)
    session.add(patient)
    await session.commit()
    await session.refresh(patient)
    return patient


async def update_patient(
    session: AsyncSession,
    patient_id: uuid.UUID,
    data: PatientUpdate,
    principal: "Principal",
) -> Patient:
    """PATCH de la ficha (admin). `description`/`allergies` son la historia clínica del
    paciente: solo los escribe el médico habilitado que lo trata (mismo criterio que la lectura,
    `clinical_grants`). El admin edita el resto de la ficha; con uno de esos campos, 403, igual
    que el motivo y las notas en el PATCH de la consulta."""
    patient = await get_patient(session, patient_id)
    changes = data.model_dump(exclude_unset=True)
    if changes.keys() & _CLINICAL_UPDATE_FIELDS:
        grants = await clinical_grants(session, principal, [patient])
        if grants[patient.id] is None:
            raise ForbiddenError(
                "Solo el médico que trata al paciente escribe sus antecedentes y alergias."
            )
    if "emergency_phone" in changes:
        _ensure_emergency_phone_differs(
            changes["emergency_phone"], changes.get("phone_whatsapp", patient.phone_whatsapp)
        )
    for field, value in changes.items():
        setattr(patient, field, value)
    await audit.log_action(
        session,
        action="patient.updated",
        actor_user_id=principal.id,
        resource="patients",
        resource_id=patient.id,
        metadata={"fields": sorted(changes)},
    )
    await session.commit()
    await session.refresh(patient)
    return patient


# --- Pacientes de consultorio (alta por médico, para pedir una interconsulta) ---
#
# Segunda vía de alta de `patients`, distinta de la pública: la fila lleva
# `created_by_doctor_id` y es del médico que la creó. La pertenencia se valida ACÁ, junto a la
# query, no en el router (regla IDOR de @.claude/rules/security.md): el permiso RBAC autoriza la
# *acción*, nunca el *objeto*.


async def _own_patient(
    session: AsyncSession, patient_id: uuid.UUID, principal: "Principal", ip: str | None
) -> Patient:
    """El paciente de consultorio del médico que llama. 404 si no existe o está archivado;
    403 (auditado) si existe pero es de otro médico (o es un alta pública, sin dueño médico)."""
    patient = await get_patient(session, patient_id)  # 404 incluye el archivado
    if patient.created_by_doctor_id != principal.id:
        await _deny(session, principal, ip, patient.id, "Este paciente no fue registrado por vos.")
    return patient


async def create_doctor_patient(
    session: AsyncSession, data: DoctorPatientCreate, doctor_id: uuid.UUID
) -> Patient:
    """Da de alta un paciente de consultorio a nombre del médico que llama."""
    if not data.consent:
        raise BadRequestError(
            "Se requiere declarar el consentimiento del paciente (consent = true)."
        )
    _ensure_emergency_phone_differs(data.emergency_phone, data.phone_whatsapp)
    patient = Patient(**data.model_dump(), created_by_doctor_id=doctor_id)
    patient.consent_at = datetime.now(UTC)
    session.add(patient)
    await session.flush()
    # Se audita el alta (la pública no lo hace): acá un miembro del staff crea PII de un tercero
    # que no está en la plataforma y no puede reclamar por sí mismo.
    await audit.log_action(
        session,
        action="patient.created_by_doctor",
        actor_user_id=doctor_id,
        resource="patients",
        resource_id=patient.id,
    )
    await session.commit()
    await session.refresh(patient)
    return patient


async def list_doctor_patients(
    session: AsyncSession, doctor_id: uuid.UUID, skip: int = 0, limit: int = 100
) -> list[Patient]:
    """Los pacientes de consultorio del médico que llama (no archivados)."""
    stmt = (
        select(Patient)
        .where(Patient.created_by_doctor_id == doctor_id, Patient.deleted_at.is_(None))
        # `id` como desempate: sin una columna única al final, dos altas del mismo instante
        # pueden repetirse u omitirse entre páginas con OFFSET.
        .order_by(Patient.created_at.desc(), Patient.id)
        .offset(skip)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_doctor_patient(
    session: AsyncSession, patient_id: uuid.UUID, *, principal: "Principal", ip: str | None = None
) -> Patient:
    return await _own_patient(session, patient_id, principal, ip)


async def update_doctor_patient(
    session: AsyncSession,
    patient_id: uuid.UUID,
    data: DoctorPatientUpdate,
    *,
    principal: "Principal",
    ip: str | None = None,
) -> Patient:
    doctor_id = principal.id
    patient = await _own_patient(session, patient_id, principal, ip)
    changes = data.model_dump(exclude_unset=True)
    if "emergency_phone" in changes:
        _ensure_emergency_phone_differs(
            changes["emergency_phone"], changes.get("phone_whatsapp", patient.phone_whatsapp)
        )
    for field, value in changes.items():
        setattr(patient, field, value)
    await audit.log_action(
        session,
        action="patient.updated",
        actor_user_id=doctor_id,
        resource="patients",
        resource_id=patient.id,
        metadata={"fields": sorted(changes)},
    )
    await session.commit()
    await session.refresh(patient)
    return patient


async def delete_doctor_patient(
    session: AsyncSession, patient_id: uuid.UUID, *, principal: "Principal", ip: str | None = None
) -> None:
    """Baja lógica del paciente propio. Nunca hard delete (igual que el resto de la tabla)."""
    doctor_id = principal.id
    patient = await _own_patient(session, patient_id, principal, ip)
    patient.deleted_at = func.now()
    await audit.log_action(
        session,
        action="patient.deleted",
        actor_user_id=doctor_id,
        resource="patients",
        resource_id=patient.id,
    )
    await session.commit()


async def delete_patient(
    session: AsyncSession, patient_id: uuid.UUID, actor_user_id: uuid.UUID | None = None
) -> None:
    """Baja lógica (soft delete): marca deleted_at, no borra la fila (trazabilidad). Mismo patrón
    que delete_doctor. get_patient ya devuelve 404 si el paciente estaba archivado."""
    patient = await get_patient(session, patient_id)
    patient.deleted_at = func.now()
    await audit.log_action(
        session,
        action="patient.deleted",
        actor_user_id=actor_user_id,
        resource="patients",
        resource_id=patient.id,
    )
    await session.commit()


# --- Dirección cifrada E2E (v1:base64 sealed box) ---
#
# La dirección se cifra en el navegador del paciente con la clave pública clínica (X25519
# sealed box). La API NUNCA la descifra, ni la loguea, ni la valida: solo almacena y
# sirve la ciphertext a quien esté autorizado (médico tratante o allowlist).
#
# Autorización para ver la dirección:
# - Email del principal en `settings.address_viewer_emails` (allowlist configurable, default:
#   la responsable de protección de datos).
# - O el principal es el médico asignado a ALGUNA consulta de este paciente (consulta viva:
#   cualquier estado sirve mientras `assigned_doctor_id == viewer_id`).
#
# Si no se cumple ninguna, se lanza ForbiddenError. Antes de devolver la ciphertext se
# escribe auditoría con action="patient.address_revealed" y metadata indicando la vía
# ("allowlist" o "tratante").


async def patient_address_for_viewer(
    session: AsyncSession, patient: Patient, *, viewer_id: uuid.UUID, viewer_email: str | None
) -> str | None:
    """Devuelve `patient.address_encrypted` si el viewer está autorizado; si no, 403.

    Autorizado si:
    - `viewer_email` (en minúsculas) está en `settings.address_viewer_emails`, O
    - Existe una consulta del paciente con `assigned_doctor_id == viewer_id`.

    Escribe auditoría ANTES de devolver la ciphertext
    (action="patient.address_revealed", metadata con `via: "allowlist" | "tratante"`).
    Commitea la transacción.
    """
    email_lower = (viewer_email or "").strip().lower()
    via = None

    # 1) Allowlist configurable (super admin / DPO).
    if email_lower and email_lower in settings.address_viewer_emails:
        via = "allowlist"

    # 2) Médico tratante: alguna consulta asignada a este viewer.
    if via is None:
        existe = await session.scalar(
            select(1)
            .where(
                Consultation.patient_id == patient.id,
                Consultation.assigned_doctor_id == viewer_id,
            )
            .limit(1)
        )
        if existe is not None:
            via = "tratante"

    if via is None:
        raise ForbiddenError("Solo el médico que atiende el caso puede ver la dirección.")

    # Auditoría ANTES de devolver la ciphertext.
    await audit.log_action(
        session,
        action="patient.address_revealed",
        actor_user_id=viewer_id,
        resource="patients",
        resource_id=patient.id,
        metadata={"via": via},
    )
    await session.commit()

    return patient.address_encrypted
