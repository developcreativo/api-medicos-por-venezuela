"""Quién puede leer el contenido clínico de un caso, y la traza de cada lectura.

Modelo (RBAC decide la acción, estos atributos deciden el objeto):
- Paciente dueño: SUMMARY de lo suyo (su motivo, sus antecedentes). Nunca las notas del médico.
- Médico habilitado asignado al caso: SUMMARY + NOTES (equipo tratante).
- Médico invitado a una interconsulta / especialista que tomó la solicitud: igual que el
  tratante, para ESE caso (decisión de producto 2026-09-23).
- Médico habilitado cuya cola incluye un caso SIN asignar: SUMMARY, para decidir si lo toma.
- Admin / super_admin: nada, con UNA excepción: en la cola del panel (``panel_queue_grant``)
  recibe SUMMARY de los casos en espera, porque la gestiona y necesita el motivo para triar. El
  listado de consultas, el detalle, la cadena y los eventos le siguen dando ``none``.
  Un admin que además ejerce como médico recibe lo que le toca COMO médico, no como admin.

Cada lectura concedida escribe `READ_CLINICAL_DATA` en `audit_log` con actor, ids, vía, IP y
correlation id. Un intento denegado sobre un recurso concreto (403) también se registra, con
`outcome = "denied"`.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.observability import correlation_id_ctx
from src.core.security import Principal
from src.schemas.clinical import ClinicalGrant, summary_grant, treating_grant
from src.services import queue_access
from src.services.audit import log_action

READ_CLINICAL_DATA = "READ_CLINICAL_DATA"


def practices_medicine(principal: Principal) -> bool:
    """Ejerce como médico con credencial válida y cuenta activa. Un admin+médico sin ficha
    habilitada NO pasa: el gate de credencial no le frena la operación, pero esto sí."""
    return principal.practices_medicine and principal.is_staff


def treating_doctor_grant(
    principal: Principal, assigned_doctor_id: uuid.UUID | None
) -> ClinicalGrant | None:
    """Equipo tratante: médico habilitado y asignado. El admin no pasa por aquí por ser admin."""
    if practices_medicine(principal) and assigned_doctor_id == principal.id:
        return treating_grant("assigned_doctor")
    return None


def interconsultation_grant(principal: Principal) -> ClinicalGrant | None:
    """El servicio ya comprobó que el caller es el invitado/el que tomó la solicitud."""
    return treating_grant("interconsultation") if practices_medicine(principal) else None


def patient_owner_grant() -> ClinicalGrant:
    """El servicio ya filtró por `patients.user_id = caller`."""
    return summary_grant("patient_owner")


async def queue_grant(db: AsyncSession, principal: Principal) -> "queue_access.QueueScope | None":
    """Alcance de cola del principal COMO MÉDICO (sin la vista global de admin). None si no
    ejerce. Se usa con `grant_for_queue_item` para decidir caso a caso."""
    if not practices_medicine(principal):
        return None
    return await queue_access.queue_scope(
        db, user_id=principal.id, specialty_id=principal.specialty_id, is_admin=False
    )


def grant_for_queue_item(
    principal: Principal,
    scope: "queue_access.QueueScope | None",
    *,
    assigned_doctor_id: uuid.UUID | None,
    specialty_id: uuid.UUID | None,
    status: str,
) -> ClinicalGrant | None:
    """Tratante si es suyo; SUMMARY si está EN ESPERA sin asignar y en su cola; si no, nada.

    `status` cuenta: un caso cancelado o cerrado que quedó sin médico no está en la cola de
    nadie (nadie va a decidir si lo toma), así que no hay necesidad de saber que lo justifique."""
    if (grant := treating_doctor_grant(principal, assigned_doctor_id)) is not None:
        return grant
    if (
        scope is not None
        and status == "waiting"
        and assigned_doctor_id is None
        and scope.allows(specialty_id)
    ):
        return summary_grant("queue_scope")
    return None


def panel_queue_grant(
    principal: Principal,
    scope: "queue_access.QueueScope | None",
    *,
    assigned_doctor_id: uuid.UUID | None,
    specialty_id: uuid.UUID | None,
    status: str,
) -> ClinicalGrant | None:
    """Grant de una fila de la COLA DEL PANEL. Igual que `grant_for_queue_item`, pero el admin
    —que ve todas las colas para gestionarlas— recibe SUMMARY en los casos en espera: necesita el
    motivo para triar y derivar (decisión de producto 2026-09-27).

    Es una excepción ACOTADA al panel: el listado de consultas (incluido /admin/pacientes), el
    detalle, la cadena y los eventos le siguen dando `none`."""
    if principal.is_admin and status == "waiting" and assigned_doctor_id is None:
        return summary_grant("admin_queue")
    return grant_for_queue_item(
        principal,
        scope,
        assigned_doctor_id=assigned_doctor_id,
        specialty_id=specialty_id,
        status=status,
    )


async def audit_clinical_read(
    db: AsyncSession,
    *,
    principal: Principal | None = None,
    ip: str | None,
    resource: str,
    grants: Iterable[tuple[uuid.UUID, ClinicalGrant | None]],
) -> None:
    """Registra las lecturas concedidas de una respuesta. Una entrada por vía de acceso: una
    lectura de detalle queda con `resource_id`; un listado (el panel) queda en una sola fila con
    los ids en `metadata.ids`, para no escribir cien filas cada vez que el panel refresca.

    Para saber quién leyó el caso X:
        where action = 'READ_CLINICAL_DATA'
          and (resource_id = 'X' or metadata->'ids' ? 'X')

    Commitea: se llama desde lecturas, que no tienen otra transacción que cierre la entrada."""
    by_reason: dict[str, tuple[ClinicalGrant, list[str]]] = {}
    for resource_id, grant in grants:
        if grant is not None:
            by_reason.setdefault(grant.reason, (grant, []))[1].append(str(resource_id))
    if not by_reason:
        return
    for reason, (grant, ids) in by_reason.items():
        await log_action(
            db,
            action=READ_CLINICAL_DATA,
            actor_user_id=principal.id if principal is not None else None,
            resource=resource,
            resource_id=ids[0] if len(ids) == 1 else None,
            metadata={
                "outcome": "granted",
                "via": reason,
                "tiers": sorted(grant.tiers),
                "ids": ids,
            },
            ip=ip,
            correlation_id=correlation_id_ctx.get(),
        )
    await db.commit()


async def audit_clinical_denied(
    db: AsyncSession,
    *,
    principal: Principal,
    ip: str | None,
    resource: str,
    resource_id: uuid.UUID,
) -> None:
    """Intento de leer el contenido clínico de un caso ajeno (el caller recibirá 403)."""
    await log_action(
        db,
        action=READ_CLINICAL_DATA,
        actor_user_id=principal.id,
        resource=resource,
        resource_id=str(resource_id),
        metadata={"outcome": "denied", "ids": [str(resource_id)]},
        ip=ip,
        correlation_id=correlation_id_ctx.get(),
    )
    await db.commit()
