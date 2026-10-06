"""Pruebas del recurso consultations y sus eventos (CRUD aislado)."""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.audit_log import AuditLog
from src.models.consultation import Consultation
from src.models.patient import Patient
from src.models.profile import Profile
from src.models.specialty import Specialty
from tests._helpers import (
    GENERAL,
    add_doctor,
    any_specialty_id,
    auth_headers,
    grant_roles,
    make_profile,
    specialty_id_by_name,
    valid_patient_payload,
)

PREFIX = "/api/v1"


async def _create_patient(client: AsyncClient, full_name: str = "Paciente Test") -> str:
    resp = await client.post(f"{PREFIX}/patients", json=valid_patient_payload(full_name=full_name))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_consultation_crud_and_code_autogeneration(client: AsyncClient) -> None:
    patient_id = await _create_patient(client)

    # code omitido -> lo genera el trigger generate_consultation_code.
    resp = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": patient_id,
            "chief_complaint": "Dolor de cabeza",
            "specialty_id": await any_specialty_id(client),
        },
    )
    assert resp.status_code == 201, resp.text
    consultation = resp.json()
    assert consultation["code"]  # autogenerado
    assert consultation["status"] == "waiting"
    cid = consultation["id"]

    # Get
    assert (await client.get(f"{PREFIX}/consultations/{cid}")).status_code == 200

    # List con filtros
    listed = await client.get(
        f"{PREFIX}/consultations", params={"status": "waiting", "patient_id": patient_id}
    )
    assert listed.status_code == 200
    assert any(c["id"] == cid for c in listed.json())

    # Patch estado (el client es admin: gestiona el estado, no las notas clínicas).
    patched = await client.patch(f"{PREFIX}/consultations/{cid}", json={"status": "closed"})
    assert patched.status_code == 200
    assert patched.json()["status"] == "closed"

    # Delete
    assert (await client.delete(f"{PREFIX}/consultations/{cid}")).status_code == 204
    assert (await client.get(f"{PREFIX}/consultations/{cid}")).status_code == 404

    audit_resp = await client.get(f"{PREFIX}/audit-log", params={"resource": "consultations"})
    entries = [e for e in audit_resp.json() if e["resource_id"] == cid]
    assert sorted(e["action"] for e in entries) == sorted(
        ["consultation.updated", "consultation.deleted"]
    )


async def test_admin_pacientes_list_enrichment_and_admin_fields(
    client: AsyncClient, admin_identity: Profile
) -> None:
    """La lista de consultas (staff) trae el paciente anidado y los campos de gestión admin
    (admin_seguimiento / nota_admin), para que el panel admin/pacientes no lea `patients` /
    `consultations` directo de Supabase."""
    created = await client.post(
        f"{PREFIX}/patients",
        json=valid_patient_payload(
            full_name="Ana Admin Caso",
            phone_whatsapp="+58412999111",
            affected_zone="Zulia",
            cedula="V-12345678",
            email="ana.caso@example.com",
        ),
    )
    patient_id = created.json()["id"]
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": patient_id, "specialty_id": await any_specialty_id(client)},
        )
    ).json()["id"]

    # La lista trae el paciente anidado con los datos que muestra/filtra la tabla admin.
    listed = await client.get(f"{PREFIX}/consultations", params={"patient_id": patient_id})
    assert listed.status_code == 200
    row = next(c for c in listed.json() if c["id"] == cid)
    assert row["patient"]["full_name"] == "Ana Admin Caso"
    assert row["patient"]["cedula"] == "V-12345678"
    assert row["patient"]["email"] == "ana.caso@example.com"
    assert row["patient"]["phone_whatsapp"] == "+58412999111"
    assert row["admin_seguimiento"] is None
    assert row["nota_admin"] is None

    # PATCH de los campos de gestión admin (admin_seguimiento es FK a users(id)).
    patched = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"admin_seguimiento": str(admin_identity.id), "nota_admin": "Revisar en 48h"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["admin_seguimiento"] == str(admin_identity.id)
    assert patched.json()["nota_admin"] == "Revisar en 48h"

    # Persistió: la lista los refleja.
    relisted = await client.get(f"{PREFIX}/consultations", params={"patient_id": patient_id})
    row2 = next(c for c in relisted.json() if c["id"] == cid)
    assert row2["admin_seguimiento"] == str(admin_identity.id)
    assert row2["nota_admin"] == "Revisar en 48h"


async def test_patient_view_exposes_scheduled_at(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """La vista del paciente (GET /consultations no-staff) expone scheduled_at para el feed de
    citas de mi-caso, y NO incluye las notas del staff (vista reducida)."""
    me = make_profile(role="patient")
    db_session.add(me)
    patient = Patient(
        full_name="Paciente Agenda",
        phone_whatsapp="+58412000200",
        affected_zone="Caracas",
        needs_tags=[],
        consent=True,
        user_id=me.id,
    )
    db_session.add(patient)
    await db_session.flush()
    # Se crea por el endpoint (code/queued_at los pone el backend) y luego se agenda.
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": str(patient.id), "specialty_id": await any_specialty_id(client)},
        )
    ).json()["id"]
    cons = await db_session.get(Consultation, uuid.UUID(cid))
    assert cons is not None
    cons.scheduled_at = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
    await db_session.flush()

    resp = await client.get(f"{PREFIX}/consultations", headers=auth_headers(me.id))
    assert resp.status_code == 200, resp.text
    row = next(c for c in resp.json() if c["id"] == cid)
    assert row["scheduled_at"] is not None
    assert "internal_note" not in row  # vista reducida del paciente


async def test_consultation_invalid_patient(client: AsyncClient) -> None:
    resp = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": "00000000-0000-0000-0000-000000000000",
            "specialty_id": await any_specialty_id(client),
        },
    )
    assert resp.status_code == 400


async def test_consultation_requiere_specialty_id(client: AsyncClient) -> None:
    """`specialty_id` es obligatorio: es la columna con la que matchea la cola.

    Cuando era opcional entraban filas sin especialidad y el backend caia a un mapa de nombres
    hardcodeado que se desincronizaba del catalogo. El mapa se elimino y las filas historicas se
    rellenaron con una migracion, asi que la columna no puede volver a quedar vacia por la puerta
    de entrada.
    """
    patient_id = await _create_patient(client)

    sin_especialidad = await client.post(
        f"{PREFIX}/consultations", json={"patient_id": patient_id}
    )
    assert sin_especialidad.status_code == 422, sin_especialidad.text

    # Una especialidad que no existe en el catalogo tampoco pasa (400, no 500).
    inexistente = await client.post(
        f"{PREFIX}/consultations",
        json={"patient_id": patient_id, "specialty_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert inexistente.status_code == 400, inexistente.text


async def test_consultation_invalid_status(client: AsyncClient) -> None:
    patient_id = await _create_patient(client)
    resp = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": patient_id,
            "status": "no_existe",
            "specialty_id": await any_specialty_id(client),
        },
    )
    assert resp.status_code == 422


async def test_consultation_code_is_server_generated(client: AsyncClient) -> None:
    patient_id = await _create_patient(client)

    # `code` no es un campo aceptado (extra="forbid"): se rechaza con 422.
    bad = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": patient_id,
            "code": "NO-ACEPTADO",
            "specialty_id": await any_specialty_id(client),
        },
    )
    assert bad.status_code == 422

    # Sin enviar `code`, el trigger de la base genera el código automáticamente.
    resp = await client.post(
        f"{PREFIX}/consultations",
        json={"patient_id": str(patient_id), "specialty_id": await any_specialty_id(client)},
    )
    assert resp.status_code == 201
    assert resp.json()["code"].startswith("CONS-")


async def test_consultation_not_found(client: AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"{PREFIX}/consultations/{missing}")).status_code == 404
    assert (
        await client.patch(f"{PREFIX}/consultations/{missing}", json={"status": "closed"})
    ).status_code == 404
    assert (await client.delete(f"{PREFIX}/consultations/{missing}")).status_code == 404
    assert (await client.get(f"{PREFIX}/consultations/{missing}/events")).status_code == 404


async def test_consultation_events(client: AsyncClient) -> None:
    patient_id = await _create_patient(client)
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": patient_id, "specialty_id": await any_specialty_id(client)},
        )
    ).json()["id"]

    # Crear evento (consultation_id coincide). El client es admin: registra eventos SIN nota
    # (la nota es clínica y solo la escribe el médico tratante).
    ok = await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": cid, "event_type": "status_change"},
    )
    assert ok.status_code == 201, ok.text

    # Mismatch de consultation_id -> 400
    other = str(uuid.uuid4())
    bad = await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": other, "event_type": "status_change"},
    )
    assert bad.status_code == 400

    # Listar
    listed = await client.get(f"{PREFIX}/consultations/{cid}/events")
    assert listed.status_code == 200
    assert len(listed.json()) == 1


# --- specialty_id (reemplaza needs_tags para el matching del panel, aparte) ---


async def test_create_consultation_con_specialty_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient_id = await _create_patient(client)
    specialty = (await db_session.execute(select(Specialty).limit(1))).scalar_one()

    resp = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": patient_id,
            "chief_complaint": "Control de rutina",
            "specialty_id": str(specialty.id),
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["specialty_id"] == str(specialty.id)


async def test_create_consultation_specialty_id_inexistente_falla_400(
    client: AsyncClient,
) -> None:
    patient_id = await _create_patient(client)
    resp = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": patient_id,
            "specialty_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert resp.status_code == 400


# --- Panel médico: claim atómico + cola (Realtime lo consume del backend) ---


async def _create_waiting_consultation(client: AsyncClient) -> str:
    """Crea una consulta en espera. Sin envejecerla: el panel ya no tiene gate de 20 min, así
    que una consulta recién creada debe aparecer en la cola de inmediato (tiempo real)."""
    patient_id = await _create_patient(client, full_name="Paciente Consulta")
    return (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": patient_id, "specialty_id": await any_specialty_id(client)},
        )
    ).json()["id"]


async def test_claim_es_atomico_solo_gana_un_medico(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Dos médicos toman el mismo caso: el primero 200, el segundo 409 (nunca ambos)."""
    cid = await _create_waiting_consultation(client)
    d1 = await add_doctor(db_session, specialty=GENERAL)
    d2 = await add_doctor(db_session, specialty=GENERAL)

    r1 = await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(d1.id)
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["assigned_doctor_id"] == str(d1.id)
    assert r1.json()["status"] == "in_progress"

    r2 = await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(d2.id)
    )
    assert r2.status_code == 409, r2.text


async def test_claim_por_whatsapp_se_rechaza(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """La atención es siempre por videoconsulta: el panel anterior aún podía mandar
    `via_whatsapp: true` y eso dejaba casos tomados sin sala. Ahora es 422 y el caso sigue en la
    cola."""
    cid = await _create_waiting_consultation(client)
    doc = await add_doctor(db_session, specialty=GENERAL)

    resp = await client.post(
        f"{PREFIX}/consultations/{cid}/claim",
        json={"via_whatsapp": True},
        headers=auth_headers(doc.id),
    )
    assert resp.status_code == 422, resp.text
    consultation = await db_session.get(Consultation, uuid.UUID(cid))
    await db_session.refresh(consultation)
    assert consultation.assigned_doctor_id is None
    assert consultation.status == "waiting"


async def test_claim_crea_la_sala_en_la_misma_toma(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Sin cuerpo y sin sala previa: el claim deja el caso tomado CON sala. Crear la sala en otra
    llamada antes del claim es lo que dejaba casos sin enlace cuando esa llamada fallaba."""
    cid = await _create_waiting_consultation(client)
    doc = await add_doctor(db_session, specialty=GENERAL)

    resp = await client.post(f"{PREFIX}/consultations/{cid}/claim", headers=auth_headers(doc.id))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "in_progress"
    assert body["attended_via_whatsapp"] is False
    assert "/vamed-" in body["video_room_url"]


async def test_claim_conserva_la_sala_que_ya_tenia(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Si el paciente ya tenía sala (se la dieron al registrarse), el médico entra a esa misma:
    otra sala los dejaría en videollamadas distintas."""
    cid = await _create_waiting_consultation(client)
    consultation = await db_session.get(Consultation, uuid.UUID(cid))
    consultation.video_room_url = "https://meet.medicosporvenezuela.org/vamed-previa"
    await db_session.flush()
    doc = await add_doctor(db_session, specialty=GENERAL)

    resp = await client.post(f"{PREFIX}/consultations/{cid}/claim", headers=auth_headers(doc.id))

    assert resp.status_code == 200, resp.text
    assert resp.json()["video_room_url"] == "https://meet.medicosporvenezuela.org/vamed-previa"


async def test_claim_de_un_caso_que_ya_no_esta_en_espera_es_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un caso cerrado y sin médico (lo cerró un admin) no se reabre por el claim."""
    cid = await _create_waiting_consultation(client)
    consultation = await db_session.get(Consultation, uuid.UUID(cid))
    consultation.status = "closed_by_admin"
    await db_session.flush()
    doc = await add_doctor(db_session, specialty=GENERAL)

    resp = await client.post(f"{PREFIX}/consultations/{cid}/claim", headers=auth_headers(doc.id))

    assert resp.status_code == 409, resp.text


# --- El correo "tu médico ya está en la sala" (lo dispara el claim por video) ---
#
# El paciente ve el botón de entrar solo en `/sala-espera`, la pantalla a la que cae justo
# después de registrarse: `/mi-caso` no muestra la sala. Quien cerró esa pestaña se quedaba
# sin forma de volver y el médico entraba a una sala vacía. Estos tests fijan que el correo
# salga exactamente cuando hay alguien esperando del otro lado, y no en los demás casos.


@contextmanager
def _capturar_aviso_de_video():
    """Dobla el envío del aviso de videoconsulta y devuelve lo que se le pasó.

    Se parchea `send_video_ready_email` en `notifications` —el nombre que el router encola—
    y no `mail.send_mail`: el BackgroundTask referencia la función del módulo, así que es ese
    el nombre que hay que sustituir.
    """
    enviados: list[dict] = []

    async def _fake(**kwargs) -> bool:
        enviados.append(kwargs)
        return True

    with patch("src.services.notifications.send_video_ready_email", AsyncMock(side_effect=_fake)):
        yield enviados


async def _waiting_con_correo_y_sala(
    client: AsyncClient, db_session: AsyncSession
) -> Consultation:
    """Consulta en espera de un paciente CON correo y con la sala ya creada, que es el estado
    real en el momento del claim: el panel llama a `/video-room` antes de tomar el caso."""
    patient = Patient(
        full_name="Paciente Con Correo",
        phone_whatsapp="+584140000099",
        email="paciente@example.com",
        affected_zone="Caracas",
        consent=True,
        emergency_phone="+584240000099",  # Distinto del WhatsApp
        address_encrypted="v1:dGVzdCBjaXBoZXJ0ZXh0",  # "test ciphertext" en base64
    )
    db_session.add(patient)
    await db_session.flush()
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": str(patient.id), "specialty_id": await any_specialty_id(client)},
        )
    ).json()["id"]
    consultation = await db_session.get(Consultation, uuid.UUID(cid))
    consultation.video_room_url = "https://meet.medicosporvenezuela.org/vamed-e2e"
    await db_session.flush()
    return consultation


async def test_claim_por_video_le_manda_al_paciente_el_enlace_de_la_sala(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    consultation = await _waiting_con_correo_y_sala(client, db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)

    with _capturar_aviso_de_video() as enviados:
        resp = await client.post(
            f"{PREFIX}/consultations/{consultation.id}/claim",
            json={},
            headers=auth_headers(doc.id),
        )

    assert resp.status_code == 200, resp.text
    assert len(enviados) == 1
    aviso = enviados[0]
    assert aviso["to_email"] == "paciente@example.com"
    assert aviso["doctor_name"]  # quién le espera, no un correo anónimo
    # El enlace pasa por el SITIO y no por Jitsi: ese salto es lo que registra la entrada del
    # paciente, y sin él el médico —que ya está dentro de la sala— sigue sin saber si viene.
    assert "/entrar-videoconsulta?" in aviso["join_url"]
    assert str(consultation.id) in aviso["join_url"]


async def test_claim_sin_correo_del_paciente_no_intenta_avisar(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`patients.email` es opcional (los de consultorio suelen no tenerlo). No tener a dónde
    escribir no es un fallo: el claim responde 200 igual.

    La consulta SÍ lleva sala, aunque el correo no vaya a salir: sin ella el caso se cortaría
    antes por "no hay sala" y este test estaría midiendo el otro camino."""
    cid = await _create_waiting_consultation(client)
    consultation = await db_session.get(Consultation, uuid.UUID(cid))
    consultation.video_room_url = "https://meet.medicosporvenezuela.org/vamed-sin-correo"
    await db_session.flush()
    doc = await add_doctor(db_session, specialty=GENERAL)

    with _capturar_aviso_de_video() as enviados:
        resp = await client.post(
            f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(doc.id)
        )

    assert resp.status_code == 200, resp.text
    assert enviados == []


async def test_claim_sin_sala_previa_igual_manda_el_enlace(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El caso llega al claim sin sala: el claim la crea, así que el paciente recibe el correo
    con enlace igual. Antes este caso no avisaba (no había sala a la que invitar)."""
    patient = Patient(
        full_name="Paciente Sin Sala",
        phone_whatsapp="+584140000098",
        email="sin-sala@example.com",
        affected_zone="Caracas",
        consent=True,
    )
    db_session.add(patient)
    await db_session.flush()
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": str(patient.id), "specialty_id": await any_specialty_id(client)},
        )
    ).json()["id"]
    doc = await add_doctor(db_session, specialty=GENERAL)

    with _capturar_aviso_de_video() as enviados:
        resp = await client.post(
            f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(doc.id)
        )

    assert resp.status_code == 200, resp.text
    assert len(enviados) == 1
    assert enviados[0]["to_email"] == "sin-sala@example.com"


async def test_el_claim_sobrevive_a_un_fallo_de_correo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Best-effort: Mailtrap caído no puede impedir que un médico tome un caso. Es la promesa
    de `send_mail` y aquí se comprueba en el flujo que la necesita — un 500 en el claim dejaría
    al paciente en la cola con el médico ya dentro de la sala."""
    consultation = await _waiting_con_correo_y_sala(client, db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)

    # Se rompe el envío de DENTRO (`send_mail`), no `send_video_ready_email`: doblar la propia
    # tarea encolada sustituiría también el `@best_effort` que la blinda, y el test pasaría a
    # medir el doble en vez del código. Mismo criterio que el de altas en
    # `test_registration_mail.py`.
    boom = AsyncMock(side_effect=RuntimeError("mailtrap caído"))
    with patch("src.services.notifications.send_mail", boom):
        resp = await client.post(
            f"{PREFIX}/consultations/{consultation.id}/claim",
            json={},
            headers=auth_headers(doc.id),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "in_progress"


async def test_claim_requiere_permiso_queue_take(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    cid = await _create_waiting_consultation(client)
    patient = make_profile(role="patient")
    db_session.add(patient)
    await db_session.flush()

    resp = await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(patient.id)
    )
    assert resp.status_code == 403, resp.text


async def test_panel_devuelve_espera_mias_y_cerradas(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)

    cid_waiting = await _create_waiting_consultation(client)
    cid_mine = await _create_waiting_consultation(client)
    await client.post(
        f"{PREFIX}/consultations/{cid_mine}/claim", json={}, headers=auth_headers(doc.id)
    )
    # Una consulta con especialidad explícita: el panel debe traer el NOMBRE resuelto
    # (es la columna con la que matchea el médico en el frontend).
    # Es la especialidad del médico: con la cola por especialidad exacta, un caso de otra no le
    # aparecería.
    specs = (await client.get(f"{PREFIX}/specialties")).json()
    spec = next(s for s in specs if s["name"].lower() == GENERAL.lower())
    patient_id = await _create_patient(client)
    cid_spec = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": patient_id, "specialty_id": spec["id"]},
        )
    ).json()["id"]

    resp = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(doc.id))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    waiting_ids = {c["id"] for c in data["waiting"]}
    mine_ids = {c["id"] for c in data["mine"]}

    assert cid_waiting in waiting_ids
    assert cid_mine in mine_ids
    assert cid_mine not in waiting_ids  # ya asignada: sale de la cola de espera
    # Seguridad: la cola de espera (waiting) NO expone el nombre del paciente en la respuesta.
    # El paciente viene anidado (zona, síntomas para elegir el caso) pero SIN `full_name`.
    item = next(c for c in data["waiting"] if c["id"] == cid_waiting)
    assert "full_name" not in item["patient"]
    # Ni con qué identificarlo o contactarlo: la cédula y el teléfono viajaban en el JSON de la
    # cola aunque el panel no los pintara, así que cualquier médico los tenía de TODOS los casos.
    assert "cedula" not in item["patient"]
    assert "phone_whatsapp" not in item["patient"]
    # La descripción y lo clínico para decidir sí están.
    assert {"description", "allergies", "affected_zone", "age_range"} <= item["patient"].keys()
    # `specialty_id` es obligatorio al crear, asi que el panel SIEMPRE resuelve el nombre; ya no
    # existe el caso "sin especialidad" que caia al matching legacy.
    assert item["specialty"] is not None
    item_spec = next(c for c in data["waiting"] if c["id"] == cid_spec)
    assert item_spec["specialty"] == spec["name"]  # el panel resuelve el NOMBRE, no el id
    # Mis consultas (ya tomadas por el médico) SÍ traen el nombre del paciente.
    mine_item = next(c for c in data["mine"] if c["id"] == cid_mine)
    assert mine_item["patient"]["full_name"] == "Paciente Consulta"
    assert {"cedula", "phone_whatsapp"} <= mine_item["patient"].keys()
    assert isinstance(data["my_closed_count"], int)
    assert data["queue_blocked_reason"] is None


async def test_panel_requiere_permiso_queue_read(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient = make_profile(role="patient")
    db_session.add(patient)
    await db_session.flush()

    resp = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(patient.id))
    assert resp.status_code == 403, resp.text


# --- Anti-IDOR: pertenencia en update/close (security.md) ---


async def _consultation_assigned_to(
    client: AsyncClient, db_session: AsyncSession, doctor_id: str
) -> str:
    """Consulta asignada a `doctor_id` (asignada por el client admin del fixture)."""
    patient_id = await _create_patient(client)
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": patient_id, "specialty_id": await any_specialty_id(client)},
        )
    ).json()["id"]
    assigned = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"status": "in_progress", "assigned_doctor_id": doctor_id},
    )
    assert assigned.status_code == 200, assigned.text
    return cid


async def test_doctor_no_puede_editar_ni_cerrar_consulta_ajena(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    dr_a = await add_doctor(db_session, specialty=GENERAL)
    dr_b = await add_doctor(db_session, specialty=GENERAL)
    cid = await _consultation_assigned_to(client, db_session, str(dr_b.id))

    headers_a = auth_headers(dr_a.id)
    patched = await client.patch(
        f"{PREFIX}/consultations/{cid}", json={"internal_note": "intruso"}, headers=headers_a
    )
    assert patched.status_code == 409

    closed = await client.post(
        f"{PREFIX}/consultations/{cid}/close", json={"outcome": "closed"}, headers=headers_a
    )
    assert closed.status_code == 409


async def test_doctor_no_puede_reasignar_a_terceros(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    dr_a = await add_doctor(db_session, specialty=GENERAL)
    dr_c = await add_doctor(db_session, specialty=GENERAL)
    patient_id = await _create_patient(client)
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": patient_id, "specialty_id": await any_specialty_id(client)},
        )
    ).json()["id"]

    # Sin asignar: A no puede asignársela a C...
    resp = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"assigned_doctor_id": str(dr_c.id)},
        headers=auth_headers(dr_a.id),
    )
    assert resp.status_code == 409

    # ...ni tomarla para sí por PATCH: sería read-then-write y reabriría la carrera
    # que el claim atómico resuelve en la base (tomar = POST /{id}/claim).
    by_patch = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"assigned_doctor_id": str(dr_a.id)},
        headers=auth_headers(dr_a.id),
    )
    assert by_patch.status_code == 409

    # La toma vía claim; ya suya, puede editarla (incluido el no-op de assigned)...
    took = await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(dr_a.id)
    )
    assert took.status_code == 200, took.text
    mine = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"assigned_doctor_id": str(dr_a.id), "internal_note": "mía"},
        headers=auth_headers(dr_a.id),
    )
    assert mine.status_code == 200, mine.text

    # ...liberarla (None) y cerrarla como propia tras re-tomarla.
    released = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"assigned_doctor_id": None},
        headers=auth_headers(dr_a.id),
    )
    assert released.status_code == 200, released.text
    await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(dr_a.id)
    )
    closed = await client.post(
        f"{PREFIX}/consultations/{cid}/close",
        json={"outcome": "closed"},
        headers=auth_headers(dr_a.id),
    )
    assert closed.status_code == 200, closed.text


async def test_doctor_no_puede_editar_doctor_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """doctor_id (ficha del médico) es server-only: un no-admin no lo edita por PATCH."""
    dr_a = await add_doctor(db_session, specialty=GENERAL)
    patient_id = await _create_patient(client)
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": patient_id, "specialty_id": await any_specialty_id(client)},
        )
    ).json()["id"]

    resp = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"doctor_id": "00000000-0000-0000-0000-000000000001"},
        headers=auth_headers(dr_a.id),
    )
    assert resp.status_code == 409


async def test_doctor_no_puede_inyectar_eventos_en_consulta_ajena(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Anti-IDOR en eventos: el historial del caso solo lo escribe el médico asignado
    (o un admin, sin nota) — sin esto, cualquier doctor podía fabricar un evento 'closed' falso."""
    dr_a = await add_doctor(db_session, specialty=GENERAL)
    dr_b = await add_doctor(db_session, specialty=GENERAL)
    cid = await _consultation_assigned_to(client, db_session, str(dr_b.id))

    payload = {"consultation_id": cid, "event_type": "closed", "note": "evento intruso"}
    intruder = await client.post(
        f"{PREFIX}/consultations/{cid}/events", json=payload, headers=auth_headers(dr_a.id)
    )
    assert intruder.status_code == 409

    owner = await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": cid, "event_type": "note", "note": "del asignado"},
        headers=auth_headers(dr_b.id),
    )
    assert owner.status_code == 201, owner.text
    assert owner.json()["note"] == "del asignado"
    assert owner.json()["clinical_access"] == "full"

    # El client del fixture es admin: registra eventos del caso, pero la nota es clínica.
    admin_note = await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": cid, "event_type": "note", "note": "del admin"},
    )
    assert admin_note.status_code == 403, admin_note.text
    admin = await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": cid, "event_type": "admin_update"},
    )
    assert admin.status_code == 201, admin.text
    assert admin.json()["clinical_access"] == "none"


async def test_admin_puede_gestionar_consulta_ajena(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    dr_b = await add_doctor(db_session, specialty=GENERAL)
    cid = await _consultation_assigned_to(client, db_session, str(dr_b.id))

    # El client del fixture es admin: gestiona estado/prioridad/nota_admin de casos ajenos...
    patched = await client.patch(
        f"{PREFIX}/consultations/{cid}", json={"priority": "review", "nota_admin": "revisar"}
    )
    assert patched.status_code == 200, patched.text
    # ...pero NO escribe (ni pisa) las notas del médico, ni con nota de cierre.
    for field in ("internal_note", "clinical_notes", "chief_complaint"):
        denied = await client.patch(f"{PREFIX}/consultations/{cid}", json={field: "admin"})
        assert denied.status_code == 403, (field, denied.text)
    with_note = await client.post(
        f"{PREFIX}/consultations/{cid}/close", json={"outcome": "closed", "note": "admin"}
    )
    assert with_note.status_code == 403, with_note.text
    closed = await client.post(f"{PREFIX}/consultations/{cid}/close", json={"outcome": "closed"})
    assert closed.status_code == 200
    assert closed.json()["clinical_access"] == "none"


# --- entered_call_at (paridad con el dashboard legacy: "en espera" = waiting +
#     el médico ya entró a la sala) ---------------------------------------------


async def test_consultation_entered_call_at_round_trips(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient_id = await _create_patient(client)
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": patient_id, "specialty_id": await any_specialty_id(client)},
        )
    ).json()["id"]

    consultation = await db_session.get(Consultation, uuid.UUID(cid))
    assert consultation.entered_call_at is None  # default: nadie ha entrado aún

    now = datetime.now(UTC)
    consultation.entered_call_at = now
    await db_session.flush()
    await db_session.refresh(consultation)
    assert consultation.entered_call_at is not None


# --- Enriquecimiento del listado (patient_name / assigned_doctor_name) --------


async def test_consultation_list_includes_patient_and_doctor_names(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient_id = await _create_patient(client, full_name="Paciente Consulta")
    doctor_profile = await add_doctor(db_session, specialty=GENERAL)

    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={
                "patient_id": patient_id,
                "chief_complaint": "Fiebre",
                "specialty_id": await any_specialty_id(client),
            },
        )
    ).json()["id"]

    patched = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"assigned_doctor_id": str(doctor_profile.id), "status": "in_progress"},
    )
    assert patched.status_code == 200, patched.text

    listed = await client.get(f"{PREFIX}/consultations", params={"patient_id": patient_id})
    assert listed.status_code == 200
    row = next(c for c in listed.json() if c["id"] == cid)
    assert row["patient_name"] == "Paciente Consulta"
    assert row["assigned_doctor_name"] == doctor_profile.full_name


async def test_consultation_list_names_are_null_when_unassigned(client: AsyncClient) -> None:
    patient_id = await _create_patient(client, full_name="Paciente Consulta")
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={
                "patient_id": patient_id,
                "chief_complaint": "Tos",
                "specialty_id": await any_specialty_id(client),
            },
        )
    ).json()["id"]

    listed = await client.get(f"{PREFIX}/consultations", params={"patient_id": patient_id})
    row = next(c for c in listed.json() if c["id"] == cid)
    assert row["patient_name"] == "Paciente Consulta"
    assert row["assigned_doctor_name"] is None


# --- Anti-PII: un paciente autenticado no debe recibir patient_name / -------
#     assigned_doctor_name (esos campos son enriquecimiento solo para staff) ---


async def test_consultation_list_hides_pii_from_patient_viewer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Guarda contra un futuro drift de `ConsultationPatientResponse`: si algún día
    se le agregan `patient_name`/`assigned_doctor_name`, este test debe fallar."""
    patient_id = await _create_patient(client)
    doctor_profile = await add_doctor(db_session, specialty=GENERAL)

    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={
                "patient_id": patient_id,
                "chief_complaint": "Fiebre",
                "specialty_id": await any_specialty_id(client),
            },
        )
    ).json()["id"]
    patched = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"assigned_doctor_id": str(doctor_profile.id), "status": "in_progress"},
    )
    assert patched.status_code == 200, patched.text

    # La cuenta (users) del paciente, ligada a su ficha (patients.user_id), es el
    # "viewer" no-staff que la API usa para filtrar por pertenencia (anti-IDOR).
    patient_profile = make_profile(role="patient")
    db_session.add(patient_profile)
    await db_session.flush()
    patient_row = await db_session.get(Patient, uuid.UUID(patient_id))
    patient_row.user_id = patient_profile.id
    await db_session.flush()

    listed = await client.get(
        f"{PREFIX}/consultations",
        params={"patient_id": patient_id},
        headers=auth_headers(patient_profile.id),
    )
    assert listed.status_code == 200, listed.text
    row = next(c for c in listed.json() if c["id"] == cid)
    assert "patient_name" not in row
    assert "assigned_doctor_name" not in row


# --- Regresión: ConsultationResponse no debe re-validar longitud de datos ------
#     ya persistidos (bug de producción: filas reales con chief_complaint > 500
#     causaban un 500 al listar/serializar). ---------------------------------


async def test_list_consultations_serializes_chief_complaint_longer_than_500(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El listado del admin ya no trae el motivo (va en null), así que la regresión se mira
    donde el motivo sí se descifra: el listado del paciente dueño."""
    patient_id = await _create_patient(client)
    long_complaint = "a" * 600  # excede el max_length=500 que tenían los esquemas de entrada.
    owner = make_profile(role="patient")
    db_session.add(owner)
    await db_session.flush()
    (await db_session.get(Patient, uuid.UUID(patient_id))).user_id = owner.id

    consultation = Consultation(
        patient_id=uuid.UUID(patient_id),
        status="in_progress",
        chief_complaint=long_complaint,
    )
    db_session.add(consultation)
    await db_session.flush()

    listed = await client.get(
        f"{PREFIX}/consultations",
        params={"status": "in_progress", "limit": 100},
        headers=auth_headers(owner.id),
    )
    assert listed.status_code == 200, listed.text
    row = next(c for c in listed.json() if c["id"] == str(consultation.id))
    assert row["chief_complaint"] == long_complaint
    assert len(row["chief_complaint"]) == 600


async def test_list_consultations_filters_by_contacted_whatsapp_status(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient_id = await _create_patient(client)

    consultation = Consultation(
        patient_id=uuid.UUID(patient_id),
        status="contacted_whatsapp",
    )
    db_session.add(consultation)
    await db_session.flush()

    listed = await client.get(f"{PREFIX}/consultations", params={"status": "contacted_whatsapp"})
    assert listed.status_code == 200, listed.text
    assert any(c["id"] == str(consultation.id) for c in listed.json())


async def test_el_panel_dice_si_el_paciente_entro_a_la_videollamada(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`entered_call_at` tiene que llegar al panel: es la ÚNICA señal duradera de que el paciente
    llegó a la sala.

    La presencia por Realtime no sirve para esto y por eso hace falta el campo: dice si el
    paciente tiene abierta una pestaña NUESTRA, y al entrar a Jitsi esa pestaña pasa a segundo
    plano —en móvil el navegador la suspende y se cae el WebSocket—, así que el médico veía
    "sin conexión" justo en el momento en que el paciente acababa de entrar.
    """
    consultation = await _waiting_con_correo_y_sala(client, db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)
    suyo = auth_headers(doc.id)

    await client.post(f"{PREFIX}/consultations/{consultation.id}/claim", json={}, headers=suyo)

    def fila(panel: dict) -> dict:
        return next(c for c in panel["mine"] if c["id"] == str(consultation.id))

    antes = (await client.get(f"{PREFIX}/consultations/panel", headers=suyo)).json()
    assert fila(antes)["entered_call_at"] is None  # todavía no ha entrado

    entrada = await client.post(
        f"{PREFIX}/consultations/{consultation.id}/entered-call", headers=suyo
    )
    assert entrada.status_code == 200, entrada.text

    despues = (await client.get(f"{PREFIX}/consultations/panel", headers=suyo)).json()
    assert fila(despues)["entered_call_at"] is not None


async def test_detalle_expone_emergencia_y_flag_de_direccion_solo_al_tratante(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    """El detalle trae el teléfono de emergencia y `can_view_patient_address` calculado en el
    servidor: True solo para el médico asignado o un email de la allowlist. La ciphertext no
    viaja en el detalle: sale por GET /patients/{id}/address."""
    patient = Patient(
        full_name="Paciente Dirección Detalle",
        phone_whatsapp="+58412000400",
        affected_zone="Caracas",
        consent=True,
        emergency_phone="+58414000400",
        address_encrypted="v1:ZGV0YWxsZS1zZWNyZXRv",
    )
    db_session.add(patient)
    await db_session.flush()

    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": str(patient.id), "specialty_id": await any_specialty_id(client)},
        )
    ).json()["id"]
    tratante = await add_doctor(db_session, specialty=GENERAL)
    otro = await add_doctor(db_session, specialty=GENERAL)
    consultation = await db_session.get(Consultation, uuid.UUID(cid))
    consultation.assigned_doctor_id = tratante.id
    await db_session.flush()

    suyo = await client.get(f"{PREFIX}/consultations/{cid}", headers=auth_headers(tratante.id))
    assert suyo.status_code == 200, suyo.text
    body = suyo.json()
    assert body["patient"]["emergency_phone"] == "+58414000400"
    assert body["can_view_patient_address"] is True
    assert "address_encrypted" not in body
    assert "address_encrypted" not in body["patient"]

    # Un médico que no atiende el caso no puede verlo: antes recibía al paciente completo
    # (nombre, cédula, contactos). El filtro del cliente no es la frontera.
    ajeno = await client.get(f"{PREFIX}/consultations/{cid}", headers=auth_headers(otro.id))
    assert ajeno.status_code == 403
    listado = await client.get(
        f"{PREFIX}/consultations?patient_id={patient.id}", headers=auth_headers(otro.id)
    )
    assert listado.status_code == 403

    monkeypatch.setattr(settings, "ADDRESS_VIEWER_EMAILS", "detalle-viewer@example.com")
    viewer = make_profile(role="super_admin")
    viewer.email = "detalle-viewer@example.com"
    db_session.add(viewer)
    await db_session.flush()
    allow = await client.get(f"{PREFIX}/consultations/{cid}", headers=auth_headers(viewer.id))
    assert allow.status_code == 200
    assert allow.json()["can_view_patient_address"] is True
    # El equipo admin sí ve el teléfono de emergencia.
    assert allow.json()["patient"]["emergency_phone"] == "+58414000400"


async def test_el_code_no_se_trunca_al_pasar_los_10000(db_session: AsyncSession) -> None:
    """Regresión: el trigger usaba `lpad(nextval, 4, '0')`, que TRUNCA a 4 dígitos; a partir
    de 10.000 dos consultas seguidas generaban el mismo `code` y violaban la unique."""
    patient = Patient(
        full_name="Paciente Código",
        phone_whatsapp="+58412000500",
        affected_zone="Caracas",
        consent=True,
    )
    db_session.add(patient)
    await db_session.flush()

    max_seq = (
        await db_session.scalar(
            text(
                "select coalesce(max(split_part(code, '-', 3)::bigint), 0) "
                "from public.consultations"
            )
        )
    ) or 0
    target_seq = max(10000, max_seq + 1)
    # setval no es transaccional: la secuencia queda adelantada, lo cual es inocuo.
    await db_session.execute(
        text("select setval('consultation_seq', :n, false)"), {"n": target_seq}
    )

    first = Consultation(patient_id=patient.id, status="waiting")
    second = Consultation(patient_id=patient.id, status="waiting")
    db_session.add_all([first, second])
    await db_session.flush()
    # `code` lo pone el trigger: el ORM no lo tiene hasta releer la fila.
    await db_session.refresh(first)
    await db_session.refresh(second)

    assert first.code != second.code
    assert first.code.split("-")[-1] == str(target_seq)
    assert second.code.split("-")[-1] == str(target_seq + 1)


# --- Cifrado y acceso clínico por necesidad de saber (tasks/cifrado-datos-clinicos) ---------
#
# El admin gestiona el caso (estado, asignación, nota_admin) pero NUNCA recibe el texto
# clínico; el médico tratante lo ve completo; el paciente dueño ve su motivo; el médico cuya
# cola incluye un caso sin asignar ve el motivo para decidir si lo toma. Cada lectura concedida
# (y cada intento denegado sobre un caso concreto) queda en audit_log.

_READ = "READ_CLINICAL_DATA"
_TRAUMA = "Traumatología y ortopedia"


async def _caso_clinico(
    client: AsyncClient, db_session: AsyncSession, *, specialty: str = GENERAL
) -> tuple[str, Profile]:
    """Caso EN ESPERA de un paciente con cuenta, antecedentes y alergias, con motivo. Devuelve
    (id del caso, cuenta del paciente)."""
    owner = make_profile(role="patient")
    db_session.add(owner)
    await db_session.flush()
    patient = Patient(
        full_name="Paciente Clínico",
        phone_whatsapp="+58412000700",
        affected_zone="Caracas",
        consent=True,
        user_id=owner.id,
        description="Asma desde la infancia",
        allergies="Penicilina",
    )
    db_session.add(patient)
    await db_session.flush()
    resp = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": str(patient.id),
            "chief_complaint": "Dolor torácico",
            "specialty_id": str(await specialty_id_by_name(db_session, specialty)),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], owner


async def _caso_atendido(client: AsyncClient, db_session: AsyncSession) -> tuple[str, Profile]:
    """Caso tomado por su médico, con nota interna, nota clínica y un evento con nota escritos
    por él. Devuelve (id, médico tratante)."""
    cid, _ = await _caso_clinico(client, db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)
    suyo = auth_headers(doc.id)
    took = await client.post(f"{PREFIX}/consultations/{cid}/claim", headers=suyo)
    assert took.status_code == 200, took.text
    notas = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"internal_note": "Sospecha de angina", "clinical_notes": "TA 150/95"},
        headers=suyo,
    )
    assert notas.status_code == 200, notas.text
    evento = await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": cid, "event_type": "note", "note": "Pide ECG"},
        headers=suyo,
    )
    assert evento.status_code == 201, evento.text
    return cid, doc


async def _lecturas(
    db_session: AsyncSession, actor_id: uuid.UUID, *, outcome: str = "granted"
) -> list[AuditLog]:
    rows = (
        await db_session.scalars(
            select(AuditLog).where(AuditLog.action == _READ, AuditLog.actor_user_id == actor_id)
        )
    ).all()
    return [r for r in rows if r.metadata_["outcome"] == outcome]


def _sin_texto_clinico(body: dict) -> None:
    assert body["clinical_access"] == "none"
    for field in ("chief_complaint", "internal_note", "clinical_notes"):
        assert body[field] is None, field


async def test_el_admin_gestiona_el_caso_sin_ver_su_contenido_clinico(
    client: AsyncClient, db_session: AsyncSession, admin_identity: Profile
) -> None:
    cid, _ = await _caso_atendido(client, db_session)

    listado = await client.get(f"{PREFIX}/consultations", params={"status": "in_progress"})
    assert listado.status_code == 200, listado.text
    fila = next(c for c in listado.json() if c["id"] == cid)
    _sin_texto_clinico(fila)
    # Sigue viendo lo operativo: a quién está asignado, el paciente, el estado.
    assert fila["status"] == "in_progress" and fila["assigned_doctor_id"] is not None
    assert fila["patient"]["full_name"] == "Paciente Clínico"
    assert fila["patient"]["description"] is None

    detalle = await client.get(f"{PREFIX}/consultations/{cid}")
    assert detalle.status_code == 200, detalle.text
    _sin_texto_clinico(detalle.json())
    assert detalle.json()["patient"]["description"] is None

    # Cambia estado y nota_admin; la respuesta tampoco trae lo clínico.
    patched = await client.patch(
        f"{PREFIX}/consultations/{cid}", json={"status": "contacted_whatsapp", "nota_admin": "ok"}
    )
    assert patched.status_code == 200, patched.text
    _sin_texto_clinico(patched.json())
    assert patched.json()["nota_admin"] == "ok"

    # Cadena e historial: 200 (gestiona el caso) pero con el contenido en null.
    cadena = await client.get(f"{PREFIX}/consultations/{cid}/chain")
    assert cadena.status_code == 200, cadena.text
    assert all(c["chief_complaint"] is None and c["internal_note"] is None for c in cadena.json())
    assert all(c["clinical_access"] == "none" for c in cadena.json())
    eventos = await client.get(f"{PREFIX}/consultations/{cid}/events")
    assert eventos.status_code == 200, eventos.text
    assert eventos.json() and all(e["note"] is None for e in eventos.json())

    # No leyó nada clínico: no hay lectura que auditar.
    assert await _lecturas(db_session, admin_identity.id) == []


async def test_el_admin_no_escribe_notas_clinicas_pero_si_el_estado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    cid, doc = await _caso_atendido(client, db_session)

    denied = await client.patch(f"{PREFIX}/consultations/{cid}", json={"internal_note": "admin"})
    assert denied.status_code == 403, denied.text
    ok = await client.patch(f"{PREFIX}/consultations/{cid}", json={"status": "contacted_whatsapp"})
    assert ok.status_code == 200, ok.text

    # La nota del médico sigue intacta.
    detalle = await client.get(f"{PREFIX}/consultations/{cid}", headers=auth_headers(doc.id))
    assert detalle.json()["internal_note"] == "Sospecha de angina"


async def test_el_medico_tratante_ve_el_caso_completo_y_queda_auditado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    cid, doc = await _caso_atendido(client, db_session)

    detalle = await client.get(f"{PREFIX}/consultations/{cid}", headers=auth_headers(doc.id))
    assert detalle.status_code == 200, detalle.text
    body = detalle.json()
    assert body["clinical_access"] == "full"
    assert body["chief_complaint"] == "Dolor torácico"
    assert body["internal_note"] == "Sospecha de angina"
    assert body["clinical_notes"] == "TA 150/95"
    assert body["patient"]["description"] == "Asma desde la infancia"

    eventos = await client.get(
        f"{PREFIX}/consultations/{cid}/events", headers=auth_headers(doc.id)
    )
    assert "Pide ECG" in {e["note"] for e in eventos.json()}
    assert all(e["clinical_access"] == "full" for e in eventos.json())

    lecturas = [r for r in await _lecturas(db_session, doc.id) if cid in r.metadata_["ids"]]
    assert lecturas, "la lectura del tratante debe quedar en audit_log"
    del_detalle = next(r for r in lecturas if r.resource_id == cid)
    assert del_detalle.resource == "consultations"
    assert del_detalle.metadata_["via"] == "assigned_doctor"
    assert del_detalle.metadata_["tiers"] == ["notes", "summary"]
    assert del_detalle.ip  # la IP del cliente (en pruebas, la del transporte ASGI)


async def test_el_paciente_ve_su_motivo_y_nunca_las_notas(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    cid, owner = await _caso_clinico(client, db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)
    await client.post(f"{PREFIX}/consultations/{cid}/claim", headers=auth_headers(doc.id))
    await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"internal_note": "Nota del médico"},
        headers=auth_headers(doc.id),
    )
    suyo = auth_headers(owner.id)

    listado = await client.get(f"{PREFIX}/consultations", headers=suyo)
    fila = next(c for c in listado.json() if c["id"] == cid)
    detalle = (await client.get(f"{PREFIX}/consultations/{cid}", headers=suyo)).json()
    for body in (fila, detalle):
        assert body["clinical_access"] == "summary"
        assert body["chief_complaint"] == "Dolor torácico"
        assert "internal_note" not in body and "clinical_notes" not in body
        assert "Nota del médico" not in str(body)

    lecturas = await _lecturas(db_session, owner.id)
    assert {r.metadata_["via"] for r in lecturas} == {"patient_owner"}
    assert all(r.metadata_["tiers"] == ["summary"] for r in lecturas)


async def test_la_cola_del_panel_da_el_motivo_al_medico_y_al_admin(
    client: AsyncClient, db_session: AsyncSession, admin_identity: Profile
) -> None:
    """El médico cuya cola incluye el caso ve motivo, antecedentes y alergias para decidir si lo
    toma (SUMMARY). El admin ve TODAS las colas para gestionarlas, así que en el PANEL también
    recibe el motivo (decisión 2026-09-27); en el listado de consultas sigue sin contenido
    clínico."""
    cid, _ = await _caso_clinico(client, db_session)
    ajeno, _ = await _caso_clinico(client, db_session, specialty=_TRAUMA)
    doc = await add_doctor(db_session, specialty=GENERAL)

    panel = (
        await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(doc.id))
    ).json()
    fila = next(c for c in panel["waiting"] if c["id"] == cid)
    assert fila["clinical_access"] == "summary"
    assert fila["chief_complaint"] == "Dolor torácico"
    assert fila["patient"]["allergies"] == "Penicilina"
    assert fila["patient"]["description"] == "Asma desde la infancia"
    via_cola = [r for r in await _lecturas(db_session, doc.id) if cid in r.metadata_["ids"]]
    assert [r.metadata_["via"] for r in via_cola] == ["queue_scope"]

    # GET /queue (Board): mismo criterio.
    board = (await client.get(f"{PREFIX}/queue", headers=auth_headers(doc.id))).json()
    en_board = next(c for c in board if c["id"] == cid)
    assert en_board["clinical_access"] == "summary"
    assert en_board["chief_complaint"] == "Dolor torácico"
    assert en_board["internal_note"] is None

    # El admin que no ejerce ve toda la cola CON el motivo: la gestiona y lo necesita para triar.
    admin_panel = (await client.get(f"{PREFIX}/consultations/panel")).json()
    for c in admin_panel["waiting"]:
        if c["id"] in (cid, ajeno):
            assert c["clinical_access"] == "summary"
            assert c["chief_complaint"] == "Dolor torácico"
            assert c["patient"]["allergies"] == "Penicilina"
    via_admin = [
        r for r in await _lecturas(db_session, admin_identity.id) if cid in r.metadata_["ids"]
    ]
    assert [r.metadata_["via"] for r in via_admin] == ["admin_queue"]
    assert all(r.metadata_["tiers"] == ["summary"] for r in via_admin)

    # La excepción es SOLO del panel: el listado de consultas (el del panel admin) sigue sin
    # contenido clínico.
    listado = (await client.get(f"{PREFIX}/consultations")).json()
    en_listado = next(c for c in listado if c["id"] == cid)
    assert en_listado["clinical_access"] == "none"
    assert en_listado["chief_complaint"] is None

    # Un admin que además ejerce: en el panel recibe SUMMARY en TODA la cola, no solo en la suya.
    dual = await add_doctor(db_session, role="admin", specialty=GENERAL)
    await grant_roles(db_session, dual.id, ["doctor"])
    dual_panel = (
        await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(dual.id))
    ).json()
    por_id = {c["id"]: c for c in dual_panel["waiting"]}
    assert por_id[cid]["clinical_access"] == "summary"
    assert por_id[cid]["chief_complaint"] == "Dolor torácico"
    assert por_id[ajeno]["clinical_access"] == "summary"
    assert por_id[ajeno]["chief_complaint"] == "Dolor torácico"


async def test_tomar_de_la_cola_da_acceso_de_tratante(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    cid, _ = await _caso_clinico(client, db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)

    took = await client.post(f"{PREFIX}/queue/{cid}/take", headers=auth_headers(doc.id))
    assert took.status_code == 200, took.text
    assert took.json()["clinical_access"] == "full"
    assert took.json()["chief_complaint"] == "Dolor torácico"

    panel = (
        await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(doc.id))
    ).json()
    mio = next(c for c in panel["mine"] if c["id"] == cid)
    assert mio["clinical_access"] == "full"
    assert mio["patient"]["full_name"] == "Paciente Clínico"


async def test_cadena_y_eventos_de_un_caso_ajeno_son_403_y_quedan_auditados(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Brecha previa: /chain y /events no comprobaban pertenencia; cualquier médico leía las
    notas de cualquier caso por id."""
    cid, _ = await _caso_atendido(client, db_session)
    otro = await add_doctor(db_session, specialty=GENERAL)
    ajeno = auth_headers(otro.id)

    assert (
        await client.get(f"{PREFIX}/consultations/{cid}/chain", headers=ajeno)
    ).status_code == 403
    assert (
        await client.get(f"{PREFIX}/consultations/{cid}/events", headers=ajeno)
    ).status_code == 403

    denegadas = await _lecturas(db_session, otro.id, outcome="denied")
    assert len(denegadas) == 2
    assert {r.resource_id for r in denegadas} == {cid}
    assert all(r.resource == "consultations" and r.ip for r in denegadas)
    assert await _lecturas(db_session, otro.id) == []


async def test_solo_el_tratante_escribe_texto_clinico(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un médico que puede gestionar un caso sin asignar (p. ej. liberarlo) no por eso escribe
    su motivo o sus notas: eso es del médico que lo atiende."""
    cid, _ = await _caso_clinico(client, db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)
    headers = auth_headers(doc.id)

    for field in ("chief_complaint", "clinical_notes", "internal_note"):
        resp = await client.patch(
            f"{PREFIX}/consultations/{cid}", json={field: "x"}, headers=headers
        )
        assert resp.status_code == 403, (field, resp.text)
    evento = await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": cid, "event_type": "note", "note": "x"},
        headers=headers,
    )
    assert evento.status_code == 403, evento.text


async def test_en_la_base_el_texto_clinico_queda_cifrado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    cid, _ = await _caso_atendido(client, db_session)

    row = (
        await db_session.execute(
            text(
                "select chief_complaint, internal_note, clinical_notes "
                "from public.consultations where id = :id"
            ),
            {"id": cid},
        )
    ).one()
    for value in row:
        assert value.startswith("enc:v1:"), value
    assert "Dolor" not in row.chief_complaint and "angina" not in row.internal_note

    notas = (
        await db_session.scalars(
            text(
                "select note from public.consultation_events "
                "where consultation_id = :id and note is not null"
            ),
            {"id": cid},
        )
    ).all()
    assert notas and all(n.startswith("enc:v1:") for n in notas)


async def test_crear_y_la_sala_no_devuelven_texto_clinico(
    anon_client: AsyncClient, client: AsyncClient, db_session: AsyncSession
) -> None:
    """El alta es anónima y el `patient_id` del cuerpo no prueba que la ficha sea suya (el motivo
    puede salir de sus antecedentes); el token de sala tampoco da acceso al contenido clínico."""
    patient = Patient(
        full_name="Paciente Anónimo",
        phone_whatsapp="+58412000701",
        affected_zone="Caracas",
        consent=True,
        description="Diabetes tipo 2",
    )
    db_session.add(patient)
    await db_session.flush()
    created = await anon_client.post(
        f"{PREFIX}/consultations",
        json={"patient_id": str(patient.id), "specialty_id": await any_specialty_id(client)},
    )
    assert created.status_code == 201, created.text
    _sin_texto_clinico(created.json())
    cid = created.json()["id"]
    sala = {"X-Consultation-Token": created.json()["access_token"]}

    for path in ("entered-call", "video-room"):
        resp = await anon_client.post(f"{PREFIX}/consultations/{cid}/{path}", headers=sala)
        assert resp.status_code == 200, (path, resp.text)
        _sin_texto_clinico(resp.json())


async def test_admin_medico_sin_ficha_habilitada_no_lee_como_tratante(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El gate de credencial no frena a un admin para OPERAR, pero ser admin con rol de médico
    no basta para leer como tratante: hace falta la ficha habilitada. Con ella, sí."""
    cid, _ = await _caso_atendido(client, db_session)
    sin_ficha = make_profile(role="admin")
    db_session.add(sin_ficha)
    await db_session.flush()
    await grant_roles(db_session, sin_ficha.id, ["doctor"])
    con_ficha = await add_doctor(db_session, specialty=GENERAL)
    await grant_roles(db_session, con_ficha.id, ["admin"])

    for quien, esperado in ((sin_ficha, "none"), (con_ficha, "full")):
        await db_session.execute(
            text("update consultations set assigned_doctor_id = :d where id = :id"),
            {"d": quien.id, "id": cid},
        )
        body = (
            await client.get(f"{PREFIX}/consultations/{cid}", headers=auth_headers(quien.id))
        ).json()
        assert body["clinical_access"] == esperado
        assert (body["internal_note"] is None) == (esperado == "none")


async def test_el_admin_que_ejerce_no_se_asigna_casos_por_patch(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Asignarse un caso por PATCH convertía a un admin que además ejerce en médico tratante
    (acceso clínico completo) de cualquier caso, incluso de uno que atendía otro médico. Tomar un
    caso para sí es solo por el claim atómico; asignar a OTRO médico sigue siendo gestión admin."""
    dual = await add_doctor(db_session, role="admin", specialty=GENERAL)
    await grant_roles(db_session, dual.id, ["doctor"])
    suyo = auth_headers(dual.id)

    # Un caso que ya atiende otro médico: 403, la asignación no cambia y no lee sus notas.
    ajeno, doc = await _caso_atendido(client, db_session)
    robo = await client.patch(
        f"{PREFIX}/consultations/{ajeno}", json={"assigned_doctor_id": str(dual.id)}, headers=suyo
    )
    assert robo.status_code == 403, robo.text
    fila = await db_session.get(Consultation, uuid.UUID(ajeno))
    await db_session.refresh(fila)
    assert fila.assigned_doctor_id == doc.id
    detalle = (await client.get(f"{PREFIX}/consultations/{ajeno}", headers=suyo)).json()
    _sin_texto_clinico(detalle)

    # Un caso sin asignar: tampoco por PATCH...
    libre, _ = await _caso_clinico(client, db_session)
    por_patch = await client.patch(
        f"{PREFIX}/consultations/{libre}",
        json={"assigned_doctor_id": str(dual.id), "status": "in_progress"},
        headers=suyo,
    )
    assert por_patch.status_code == 403, por_patch.text

    # ...sí por el claim atómico, que lo deja como tratante.
    took = await client.post(f"{PREFIX}/consultations/{libre}/claim", headers=suyo)
    assert took.status_code == 200, took.text
    assert took.json()["clinical_access"] == "full"

    # Asignar a OTRO médico sigue siendo gestión admin.
    otro = await add_doctor(db_session, specialty=GENERAL)
    para_otro, _ = await _caso_clinico(client, db_session)
    asignado = await client.patch(
        f"{PREFIX}/consultations/{para_otro}",
        json={"assigned_doctor_id": str(otro.id), "status": "in_progress"},
        headers=suyo,
    )
    assert asignado.status_code == 200, asignado.text
    assert asignado.json()["assigned_doctor_id"] == str(otro.id)
    _sin_texto_clinico(asignado.json())


async def test_un_caso_cancelado_o_cerrado_sin_medico_no_da_motivo_por_cola(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El SUMMARY de la cola es para decidir si tomar un caso EN ESPERA. Uno cancelado o cerrado
    que quedó sin médico no está en la cola de nadie: al admin que ejerce esa especialidad le
    sale en null en el listado, aunque siga sin asignar."""
    dual = await add_doctor(db_session, role="admin", specialty=GENERAL)
    await grant_roles(db_session, dual.id, ["doctor"])

    casos: dict[str, str] = {}
    for estado in ("waiting", "cancelled", "closed"):
        cid, _ = await _caso_clinico(client, db_session)
        if estado != "waiting":
            cambio = await client.patch(f"{PREFIX}/consultations/{cid}", json={"status": estado})
            assert cambio.status_code == 200, cambio.text
        casos[estado] = cid

    for estado, cid in casos.items():
        fila_orm = await db_session.get(Consultation, uuid.UUID(cid))
        listado = await client.get(
            f"{PREFIX}/consultations",
            params={"patient_id": str(fila_orm.patient_id)},
            headers=auth_headers(dual.id),
        )
        assert listado.status_code == 200, listado.text
        (fila,) = listado.json()
        assert fila["status"] == estado and fila["assigned_doctor_id"] is None
        if estado == "waiting":
            assert fila["clinical_access"] == "summary"
            assert fila["chief_complaint"] == "Dolor torácico"
        else:
            assert fila["clinical_access"] == "none", estado
            assert fila["chief_complaint"] is None

    # Solo el caso en espera quedó como lectura concedida.
    leidos = {i for r in await _lecturas(db_session, dual.id) for i in r.metadata_["ids"]}
    assert casos["waiting"] in leidos
    assert casos["cancelled"] not in leidos and casos["closed"] not in leidos


async def test_listado_paginado_disjunto_con_desempate_por_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """ "Cargar más" en admin/pacientes pagina con skip/limit. Cinco casos del mismo paciente
    creados en la misma transacción comparten created_at: el orden entre ellos lo decide el id
    (descendente), sin repetir ni omitir ninguno entre páginas."""
    patient_id = await _create_patient(client, "Paciente Paginado")
    sembrados = []
    for _ in range(5):
        c = Consultation(patient_id=uuid.UUID(patient_id))
        db_session.add(c)
        await db_session.flush()
        sembrados.append(str(c.id))

    vistos: list[str] = []
    for skip in range(0, 6, 2):
        page = await client.get(
            f"{PREFIX}/consultations",
            params={"patient_id": patient_id, "skip": skip, "limit": 2},
        )
        assert page.status_code == 200, page.text
        vistos.extend(c["id"] for c in page.json())

    assert len(vistos) == len(set(vistos)) == 5
    assert vistos == sorted(sembrados, reverse=True)


async def test_admin_deja_la_traza_admin_update_pero_no_notas_clinicas(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Regresión 2026-09-23: "Gestionar caso" hace PATCH (entraba) y luego registra un evento
    `admin_update` con "Estado: …; médico: …". Ese evento daba 403 por llevar nota y el panel
    decía "No se pudo actualizar el caso". La traza operativa sí; una nota clínica, no."""
    cid, _ = await _caso_atendido(client, db_session)

    traza = await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={
            "consultation_id": cid,
            "event_type": "admin_update",
            "note": "Estado: Cerrada; médico: Sin asignar",
        },
    )
    assert traza.status_code == 201, traza.text
    assert traza.json()["note"] is None  # se guarda, pero se lee como nota del tratante

    clinica = await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": cid, "event_type": "note", "note": "Sospecha de angina"},
    )
    assert clinica.status_code == 403

    raw = await db_session.scalar(
        text(
            "select note from consultation_events where consultation_id = :id "
            "and event_type = 'admin_update' order by created_at desc limit 1"
        ),
        {"id": cid},
    )
    assert raw.startswith("enc:v1:")
