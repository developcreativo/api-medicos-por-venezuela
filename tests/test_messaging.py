"""Pruebas exhaustivas del módulo de mensajería (médico ↔ paciente).

Cubre:
- Permisos y pertenencia para médico y paciente (sesión y token).
- Cifrado clínico y fail-closed (el admin ve metadatos y conteos pero NO cuerpos ni adjuntos).
- Subida de adjuntos (PDF, JPG, PNG, WEBP válidos; rechazo estricto de GIF con HTTP 422).
- Descarga segura de adjuntos con nosniff y grant clínico (admin prohibido).
- Presencia asimétrica (solo el médico tratante ve si el paciente está en línea).
- Marcado como leído idempotente y contadores.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import consultation_token
from src.core.config import settings
from src.core.errors import ConflictError, UnprocessableError, UpstreamServiceError
from src.core.ratelimit import limiter
from src.core.security import Principal, get_optional_principal
from src.db.session import AsyncSessionLocal, get_session_factory
from src.main import app
from src.models.audit_log import AuditLog
from src.models.clinical import Message
from src.models.consultation import Consultation
from src.models.consultation_event import ConsultationEvent
from src.models.doctor import Doctor
from src.models.patient import Patient
from src.models.profile import Profile
from src.services import messaging, notifications, storage
from src.services.clinical_access import READ_CLINICAL_DATA
from tests._helpers import (
    GENERAL,
    add_doctor,
    any_specialty_id,
    auth_headers,
    grant_roles,
    make_doctor_row,
    make_profile,
    specialty_id_by_name,
    valid_patient_payload,
)

PREFIX = "/api/v1"

# Binary samples with valid magic bytes
PDF_BYTES = b"%PDF-1.4\n%test pdf binary content for medical report\n%%EOF"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
)
JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\xff\xd9"
)
WEBP_BYTES = (
    b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x00\x30\x01\x00\x9d\x01\x2a\x01\x00\x01\x00\x02\x00"
)
GIF_BYTES = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


@pytest.fixture(autouse=True)
def bucket_falso(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, bytes]]:
    """Supabase Storage en memoria: `httpx.MockTransport` en lugar del bucket real.

    Ejercita el cliente HTTP completo de `services/storage.py` (URL del objeto, cabeceras de
    autorización, códigos de estado) sin depender de que el contenedor de Storage del Supabase
    local esté arriba. Mismo patrón que `fake_kit` en `tests/test_marketing_performance.py`.
    """
    objetos: dict[str, bytes] = {}
    prefijo = f"/storage/v1/object/{settings.STORAGE_BUCKET_ATTACHMENTS}/"

    def handler(request: httpx.Request) -> httpx.Response:
        # El bucket es privado: sin el service-role key Storage respondería 401/403.
        assert request.headers.get("authorization", "").startswith("Bearer ")
        assert request.url.path.startswith(prefijo), request.url.path
        clave = request.url.path[len(prefijo) :]
        if request.method == "POST":
            objetos[clave] = request.content
            return httpx.Response(200, json={"Key": clave})
        if request.method == "GET":
            if clave not in objetos:
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, content=objetos[clave])
        if request.method == "DELETE":
            if objetos.pop(clave, None) is None:
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, json={"message": "ok"})
        return httpx.Response(405, json={"error": "method_not_allowed"})

    monkeypatch.setattr(storage, "_transport", httpx.MockTransport(handler))
    yield objetos


async def _create_test_case(
    client: AsyncClient,
    db: AsyncSession,
    *,
    doctor: Profile | None = None,
    patient_user_id: uuid.UUID | None = None,
    status: str = "in_progress",
) -> tuple[str, str, str]:
    """Crea un paciente y una consulta asignada a un médico."""
    p_resp = await client.post(f"{PREFIX}/patients", json=valid_patient_payload())
    assert p_resp.status_code == 201, p_resp.text
    patient_id = p_resp.json()["id"]

    if patient_user_id:
        patient = await db.get(Patient, uuid.UUID(patient_id))
        assert patient is not None
        patient.user_id = patient_user_id
        await db.flush()

    spec_id = await any_specialty_id(client)
    c_resp = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": patient_id,
            "specialty_id": spec_id,
            "chief_complaint": "Motivo clínico inicial",
        },
    )
    assert c_resp.status_code == 201, c_resp.text
    consultation_id = c_resp.json()["id"]

    consultation = await db.get(Consultation, uuid.UUID(consultation_id))
    assert consultation is not None
    consultation.status = status
    if doctor:
        consultation.assigned_doctor_id = doctor.id
    await db.flush()

    token = consultation_token.issue(consultation.id)
    return consultation_id, patient_id, token


# =====================================================================
# 1. Permisos y envío de mensajes
# =====================================================================


async def test_doctor_send_message_and_idempotency(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El médico tratante envía un mensaje y client_msg_id es idempotente."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)

    # 1. Médico tratante envía mensaje
    client_msg_id = f"msg-{uuid.uuid4()}"
    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Hola paciente, ¿cómo se siente hoy?", "client_msg_id": client_msg_id},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["body"] == "Hola paciente, ¿cómo se siente hoy?"
    assert data["direction"] == "doctor_to_patient"
    assert data["sender_role"] == "doctor"
    assert data["clinical_access"] == "full"
    first_msg_id = data["id"]

    # 2. Reenvío con el mismo client_msg_id devuelve 200 OK con el mismo mensaje
    dup_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Hola paciente, ¿cómo se siente hoy?", "client_msg_id": client_msg_id},
    )
    assert dup_resp.status_code == 200
    dup_data = dup_resp.json()
    assert dup_data["id"] == first_msg_id


async def test_doctor_outsider_cannot_send_message(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un médico que no está en la consulta recibe 404 (no 403, CA2.1)."""
    doc_tratante = await add_doctor(db_session)
    doc_ajeno = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc_tratante)

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc_ajeno.id),
        json={"body": "Intento de intromisión"},
    )
    assert resp.status_code == 404, resp.text


async def test_doctor_cannot_send_in_waiting_or_closed_expired(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """No se puede escribir si la consulta está en waiting o cerrada hace más de 72h."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc, status="waiting")

    # 1. En waiting el médico no puede escribir
    w_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Mensaje en espera"},
    )
    assert w_resp.status_code == 409

    # 2. Cerrada hace más de 72h (MESSAGING_AFTER_CLOSE_HOURS)
    consultation = await db_session.get(Consultation, uuid.UUID(cid))
    assert consultation is not None
    consultation.status = "closed"
    consultation.closed_at = datetime.now(UTC) - timedelta(hours=73)
    await db_session.flush()

    c_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Mensaje tras cierre expirado"},
    )
    assert c_resp.status_code == 409


async def test_patient_send_message_token_and_account(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El paciente puede escribir con su token o con su cuenta de usuario."""
    doc = await add_doctor(db_session)
    patient_user_id = uuid.uuid4()
    # Perfil del paciente con cuenta
    p_profile = Profile(
        id=patient_user_id,
        full_name="Paciente Con Cuenta",
        role="patient",
        active=True,
        verified=True,
        role_chosen=True,
    )
    db_session.add(p_profile)
    await db_session.flush()

    cid, _, token = await _create_test_case(
        client, db_session, doctor=doc, patient_user_id=patient_user_id
    )

    # 1. Paciente anónimo con token de consulta
    anon_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Hola doctora, soy el paciente anónimo"},
    )
    assert anon_resp.status_code == 201, anon_resp.text
    anon_data = anon_resp.json()
    assert anon_data["sender_role"] == "patient"
    assert anon_data["direction"] == "patient_to_doctor"
    assert anon_data["sender_user_id"] is None

    # 2. Paciente autenticado con su sesión
    auth_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(patient_user_id),
        json={"body": "Hola doctora, ahora escribo desde mi cuenta"},
    )
    assert auth_resp.status_code == 201, auth_resp.text
    auth_data = auth_resp.json()
    assert auth_data["sender_user_id"] == str(patient_user_id)


async def test_patient_invalid_token_rejected(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Token expirado, manipulado o de otra consulta es rechazado."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)
    other_cid, _, other_token = await _create_test_case(client, db_session, doctor=doc)

    # Token de otra consulta
    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": other_token},
        json={"body": "Token cruzado"},
    )
    assert resp.status_code in (401, 404)

    # Sin token ni sesión
    no_auth_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        json={"body": "Sin credenciales"},
    )
    assert no_auth_resp.status_code == 401


async def test_patient_hourly_rate_limit(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El paciente tiene un límite de mensajes por hora (CA4.4)."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # Simulamos inserción de 30 mensajes recientes en la base
    for i in range(30):
        db_session.add(
            Message(
                id=uuid.uuid4(),
                consultation_id=uuid.UUID(cid),
                sender_role="patient",
                direction="patient_to_doctor",
                channel="web",
                kind="text",
                body=f"Mensaje {i}",
                sent_at=datetime.now(UTC),
                delivery_status="sent",
            )
        )
    await db_session.flush()

    # El mensaje 31 en la misma hora es rechazado con 409
    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Mensaje superando límite"},
    )
    assert resp.status_code == 409
    assert "Límite de mensajes" in resp.json()["detail"]


# =====================================================================
# 2. Cifrado clínico y fail-closed (Admin vs Médico)
# =====================================================================


async def test_clinical_grant_fail_closed_admin_vs_doctor(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Fail-closed: el admin ve metadatos pero NO el cuerpo cifrado; el médico tratante sí."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # El paciente escribe un secreto clínico
    secret_body = "Tengo fiebre alta y erupciones en la piel"
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": secret_body},
    )

    # 1. Médico tratante lee el hilo: recibe body descifrado y clinical_access='full'
    doc_resp = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
    )
    assert doc_resp.status_code == 200
    doc_body = doc_resp.json()
    assert doc_body["consultation_id"] == cid
    assert doc_body["unread_count"] == 1
    assert doc_body["clinical_access"] == "full"
    doc_data = doc_body["items"]
    assert len(doc_data) == 1
    assert doc_data[0]["body"] == secret_body
    assert doc_data[0]["clinical_access"] == "full"

    # 2. Administrador lee el hilo: recibe 200 con metadatos,
    # pero body es null y clinical_access='none'
    admin_resp = await client.get(f"{PREFIX}/consultations/{cid}/messages")
    assert admin_resp.status_code == 200
    admin_body = admin_resp.json()
    assert admin_body["clinical_access"] == "none"
    admin_data = admin_body["items"]
    assert len(admin_data) == 1
    assert admin_data[0]["body"] is None  # FAIL-CLOSED
    assert admin_data[0]["clinical_access"] == "none"
    assert admin_data[0]["id"] == doc_data[0]["id"]  # Metadatos sí viajan


async def test_admin_who_is_treating_doctor_has_clinical_grant(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Si el administrador es también el médico tratante asignado, recibe grant clínico."""
    admin_doc = await add_doctor(db_session)
    await grant_roles(db_session, admin_doc.id, ["admin"])
    cid, _, token = await _create_test_case(client, db_session, doctor=admin_doc)

    # El paciente envía un mensaje
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Dolor abdominal agudo"},
    )

    # El admin-médico tratante lee el hilo: debe ver el cuerpo descifrado y clinical_access='full'
    resp = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(admin_doc.id),
    )
    assert resp.status_code == 200
    data = resp.json()["items"]
    assert len(data) == 1
    assert data[0]["body"] == "Dolor abdominal agudo"
    assert data[0]["clinical_access"] == "full"

    # El admin-médico tratante puede responder en el hilo
    reply = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(admin_doc.id),
        json={"body": "¿Desde qué hora comenzó el dolor?"},
    )
    assert reply.status_code == 201
    assert reply.json()["body"] == "¿Desde qué hora comenzó el dolor?"


# =====================================================================
# 3. Subida y validación de archivos adjuntos (R15)
# =====================================================================


async def test_attachment_upload_valid_formats(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Médico y paciente pueden subir PDF, PNG, JPG y WEBP válidos."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    formats = [
        ("reporte.pdf", PDF_BYTES, "application/pdf"),
        ("radiografia.png", PNG_BYTES, "image/png"),
        ("foto.jpg", JPEG_BYTES, "image/jpeg"),
        ("examen.webp", WEBP_BYTES, "image/webp"),
    ]

    for fname, fbytes, expected_mime in formats:
        resp = await anon_client.post(
            f"{PREFIX}/consultations/{cid}/attachments",
            headers=auth_headers(doc.id),
            files={"file": (fname, fbytes, expected_mime)},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["file_name"] == fname
        assert data["mime_type"] == expected_mime
        assert data["file_size_bytes"] == len(fbytes)
        assert data["clinical_access"] == "full"


async def test_attachment_upload_gif_strictly_rejected_422(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El formato GIF está estrictamente prohibido y debe responder 422."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # 1. Extensión .gif
    resp1 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("animacion.gif", GIF_BYTES, "image/gif")},
    )
    assert resp1.status_code == 422
    assert "GIF" in resp1.json()["detail"]

    # 2. Magic bytes de GIF camuflados con otra extensión
    resp2 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("disfrazado.png", GIF_BYTES, "image/png")},
    )
    assert resp2.status_code == 422
    assert "GIF" in resp2.json()["detail"]

    # 3. Paciente intentando subir GIF con token
    resp3 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers={"X-Consultation-Token": token},
        files={"file": ("meme.gif", GIF_BYTES, "image/gif")},
    )
    assert resp3.status_code == 422
    assert "GIF" in resp3.json()["detail"]


async def test_attachment_upload_corrupt_and_size_limits(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Archivos corruptos o vacíos son rechazados con 422."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)

    # Archivo vacío
    resp_empty = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("vacio.pdf", b"", "application/pdf")},
    )
    assert resp_empty.status_code == 422

    # Archivo con bytes falsos que no coinciden con ninguna firma válida
    resp_corrupt = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("corrupto.pdf", b"esto es texto plano no un pdf", "application/pdf")},
    )
    assert resp_corrupt.status_code == 422


async def test_attachment_message_linking(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un mensaje puede vincular archivos adjuntos previamente subidos."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)

    # Subimos 2 archivos
    att1_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("doc1.pdf", PDF_BYTES, "application/pdf")},
    )
    att1_id = att1_resp.json()["id"]

    att2_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("foto.jpg", JPEG_BYTES, "image/jpeg")},
    )
    att2_id = att2_resp.json()["id"]

    # Enviamos mensaje con los dos adjuntos
    msg_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Adjunto exámenes", "attachment_ids": [att1_id, att2_id]},
    )
    assert msg_resp.status_code == 201
    msg_data = msg_resp.json()
    assert len(msg_data["attachments"]) == 2
    assert {a["id"] for a in msg_data["attachments"]} == {att1_id, att2_id}


# =====================================================================
# 4. Descarga segura con nosniff y grant clínico
# =====================================================================


async def test_attachment_download_with_grant_and_security_headers(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Descarga de adjuntos: médico tratante y paciente dueño acceden;
    cabecera nosniff presente."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # Subir PDF
    upload_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("informe_biopsia.pdf", PDF_BYTES, "application/pdf")},
    )
    att_id = upload_resp.json()["id"]

    # 1. Médico tratante descarga
    doc_dl = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/attachments/{att_id}",
        headers=auth_headers(doc.id),
    )
    assert doc_dl.status_code == 200
    assert doc_dl.content == PDF_BYTES
    assert doc_dl.headers["x-content-type-options"] == "nosniff"
    assert "inline" in doc_dl.headers["content-disposition"]
    assert "informe_biopsia.pdf" in doc_dl.headers["content-disposition"]

    # 2. Paciente dueño descarga con token
    pat_dl = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/attachments/{att_id}",
        headers={"X-Consultation-Token": token},
    )
    assert pat_dl.status_code == 200
    assert pat_dl.content == PDF_BYTES

    # 3. Administrador NO puede descargar adjunto clínico (403)
    admin_dl = await client.get(f"{PREFIX}/consultations/{cid}/attachments/{att_id}")
    assert admin_dl.status_code == 403

    # 4. Médico ajeno recibe 404 (CA15.4)
    doc_ajeno = await add_doctor(db_session)
    ajeno_dl = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/attachments/{att_id}",
        headers=auth_headers(doc_ajeno.id),
    )
    assert ajeno_dl.status_code == 404


# =====================================================================
# 5. Presencia asimétrica y buzón (Inbox)
# =====================================================================


async def test_asymmetric_presence_in_inbox(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El médico tratante ve si el paciente está en línea;
    el paciente nunca ve presencia médica."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # 1. Sin actividad reciente del paciente -> patient_online es False
    # Enviamos un mensaje previo del doctor para que el hilo aparezca en el inbox
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Iniciando conversación"},
    )

    inbox_resp1 = await anon_client.get(f"{PREFIX}/inbox", headers=auth_headers(doc.id))
    assert inbox_resp1.status_code == 200
    threads1 = inbox_resp1.json()
    assert len(threads1) == 1
    assert threads1[0]["patient_online"] is False

    # 2. El paciente realiza una acción (ej. envía un mensaje con su token)
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Estoy aquí doctora"},
    )

    # 3. Ahora el médico ve al paciente 'patient_online': True
    inbox_resp2 = await anon_client.get(f"{PREFIX}/inbox", headers=auth_headers(doc.id))
    threads2 = inbox_resp2.json()
    assert threads2[0]["patient_online"] is True
    assert threads2[0]["patient_last_seen_at"] is not None

    # 4. Regla de asimetría: El listado de mensajes hacia el paciente NUNCA expone presencia
    pat_msgs_resp = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
    )
    assert pat_msgs_resp.status_code == 200
    for m in pat_msgs_resp.json()["items"]:
        assert "doctor_online" not in m
        assert "doctor_last_seen_at" not in m


# =====================================================================
# 6. Marcado como leído idempotente y contadores
# =====================================================================


async def test_mark_as_read_idempotency_and_counters(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Marcar leído actualiza solo mensajes opuestos y es estrictamente idempotente."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # Paciente envía 2 mensajes
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Mensaje 1"},
    )
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Mensaje 2"},
    )

    # Médico consulta el hilo: unread_count es 2 en el CUERPO (CA3.4) y en la cabecera
    list_resp1 = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
    )
    assert list_resp1.json()["unread_count"] == 2
    assert list_resp1.headers["x-unread-count"] == "2"

    # Médico marca como leído
    read_resp1 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages/read",
        headers=auth_headers(doc.id),
    )
    assert read_resp1.status_code == 200
    assert read_resp1.json()["marked"] == 2

    # Segundo llamado consecutivo devuelve marked = 0 (idempotencia)
    read_resp2 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages/read",
        headers=auth_headers(doc.id),
    )
    assert read_resp2.status_code == 200
    assert read_resp2.json()["marked"] == 0

    # Ahora unread_count es 0
    list_resp2 = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
    )
    assert list_resp2.json()["unread_count"] == 0
    assert list_resp2.headers["x-unread-count"] == "0"


# =====================================================================
# 7. Avisos por correo con anti-ráfaga (debounce) y privacidad clínica (T1.5)
# =====================================================================


@pytest.fixture
def factory_de_prueba(db_session: AsyncSession):
    """Las sesiones cortas de los streams ven los datos de la transacción de la prueba."""

    @asynccontextmanager
    async def _session():
        yield db_session

    app.dependency_overrides[get_session_factory] = lambda: _session
    yield
    app.dependency_overrides.pop(get_session_factory, None)


async def test_messaging_email_dispatch_and_debounce(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6: Disparo de emails con anti-ráfaga de 15 min y limpieza al marcar leído."""
    doc = await add_doctor(db_session)
    doc.email = "doctor.tratante@example.com"
    await db_session.flush()

    cid, pid, token = await _create_test_case(client, db_session, doctor=doc)

    # Actualizar correo del paciente
    patient = await db_session.get(Patient, uuid.UUID(pid))
    assert patient is not None
    patient.email = "paciente@example.com"
    await db_session.flush()

    sent_mails: list[dict] = []

    async def mock_send_mail(
        to_email: str,
        subject: str,
        text: str,
        html: str = "",
        category: str = "general",
    ) -> bool:
        sent_mails.append(
            {"to": to_email, "subject": subject, "text": text, "html": html, "category": category}
        )
        return True

    monkeypatch.setattr(notifications, "send_mail", mock_send_mail)

    # 1. Paciente escribe al médico -> Correo enviado
    resp1 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Hola doctor, tengo una consulta."},
    )
    assert resp1.status_code == 201
    assert len(sent_mails) == 1
    assert sent_mails[0]["to"] == "doctor.tratante@example.com"
    assert sent_mails[0]["subject"] == "Tu paciente te escribió"
    assert "Hola doctor" not in sent_mails[0]["text"]  # Cero texto clínico

    # 2. Paciente envía 2do mensaje casi de inmediato (< 15 min, sin leer) -> Debounce bloquea
    resp2 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Se me olvidó comentarle otro detalle."},
    )
    assert resp2.status_code == 201
    assert len(sent_mails) == 1  # No aumentó

    # 3. Médico marca como leído -> Limpia debounce
    read_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages/read",
        headers=auth_headers(doc.id),
    )
    assert read_resp.status_code == 200

    # 4. Paciente escribe de nuevo -> Se envía correo porque ya no hay no-leídos pendientes
    resp3 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Gracias por responder doctor."},
    )
    assert resp3.status_code == 201
    assert len(sent_mails) == 2
    assert sent_mails[1]["subject"] == "Tu paciente te escribió"

    # 5. Médico responde al paciente -> Correo enviado al paciente
    resp4 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Con gusto, tómese el medicamento."},
    )
    assert resp4.status_code == 201
    assert len(sent_mails) == 3
    assert sent_mails[2]["to"] == "paciente@example.com"
    assert sent_mails[2]["subject"] == "Tu médico te respondió"
    assert "tómese el medicamento" not in sent_mails[2]["text"]  # Cero texto clínico


async def test_messaging_doctor_opt_out_notifications(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6.1: Si el médico desactiva `message_received` en sus preferencias, no recibe correo."""
    doc = await add_doctor(db_session)
    doc.email = "doctor.optout@example.com"
    doc.notification_prefs = {"message_received": {"email": False}}
    await db_session.flush()

    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    sent_mails: list[dict] = []

    async def mock_send_mail(
        to_email: str,
        subject: str,
        text: str,
        html: str = "",
        category: str = "general",
    ) -> bool:
        sent_mails.append({"to": to_email, "subject": subject})
        return True

    monkeypatch.setattr(notifications, "send_mail", mock_send_mail)

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Hola doctor."},
    )
    assert resp.status_code == 201
    assert len(sent_mails) == 0


# =====================================================================
# 8. Tiempo real (SSE) y presencia asimétrica (T1.6 / R8)
# =====================================================================


async def test_inbox_stream_sse_endpoint(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    factory_de_prueba: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8.2: GET /inbox/stream emite eventos `inbox` con conteos y consultas actualizadas."""
    monkeypatch.setattr(settings, "WAITING_ROOM_STREAM_MAX_SECONDS", 0)

    # 1. No autenticado -> 401
    sin_auth = await anon_client.get(f"{PREFIX}/inbox/stream")
    assert sin_auth.status_code == 401

    # 2. Sin permiso messages.read (usuario ordinario) -> 403
    unauth_resp = await anon_client.get(
        f"{PREFIX}/inbox/stream",
        headers=auth_headers(uuid.uuid4()),
    )
    assert unauth_resp.status_code == 403

    # 3. Médico con messages.read
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # Paciente envía un mensaje para generar no leídos
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Mensaje en espera"},
    )

    resp = await anon_client.get(
        f"{PREFIX}/inbox/stream",
        headers=auth_headers(doc.id),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"
    assert "event: inbox" in resp.text
    assert '"unread_total":1' in resp.text
    assert cid in resp.text
    # Cero cuerpos clínicos en el stream
    assert "Mensaje en espera" not in resp.text


async def test_waiting_room_stream_emits_message_event_and_presence_asymmetry(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    factory_de_prueba: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8.1 y CA7.2: La sala emite `message` y preserva estricta asimetría de presencia."""
    monkeypatch.setattr(settings, "WAITING_ROOM_STREAM_MAX_SECONDS", 0)
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # Médico envía mensaje al paciente
    msg_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Hola paciente, ya le atiendo."},
    )
    assert msg_resp.status_code == 201

    # Paciente se conecta al stream de la sala
    resp = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/waiting-room/stream",
        headers={"X-Consultation-Token": token},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: status" in resp.text
    assert "event: message" in resp.text
    assert '"direction":"doctor_to_patient"' in resp.text
    assert '"unread_count":1' in resp.text

    # Verifica que el paciente está marcado como en línea en memoria
    assert messaging.is_patient_online(uuid.UUID(cid)) is True

    # Asimetría estricta: La respuesta de la sala NO revela presencia del médico
    assert "doctor_online" not in resp.text
    assert "doctor_last_seen" not in resp.text
    assert "Hola paciente" not in resp.text  # Cero cuerpo clínico en SSE


# =====================================================================
# 9. Lista blanca de estados que admiten mensajes (CA2.2 / CA4.5)
# =====================================================================


async def test_estados_permitidos_para_escribir_en_el_hilo(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA2.2: `in_progress`, `scheduled` y `referred_to_specialist` admiten mensajes.

    Y `contacted_whatsapp`, añadido al conjunto por decisión del cliente (2026-10-05): es un
    caso abierto con médico asignado, y es el paciente al que el médico tuvo que dar su número
    personal — el escenario que este módulo existe para reemplazar.
    """
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)
    consulta = await db_session.get(Consultation, uuid.UUID(cid))
    assert consulta is not None

    for estado in ("in_progress", "scheduled", "referred_to_specialist", "contacted_whatsapp"):
        consulta.status = estado
        await db_session.flush()
        resp = await anon_client.post(
            f"{PREFIX}/consultations/{cid}/messages",
            headers=auth_headers(doc.id),
            json={"body": f"Mensaje con la consulta en {estado}"},
        )
        assert resp.status_code == 201, f"{estado}: {resp.text}"

    # En `contacted_whatsapp` el paciente también escribe y también adjunta: si el médico puede
    # responder pero el paciente no puede contestar, el hilo no sirve para nada.
    consulta.status = "contacted_whatsapp"
    await db_session.flush()
    del_paciente = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Le escribo por aquí en vez de por WhatsApp"},
    )
    assert del_paciente.status_code == 201, del_paciente.text
    adjunto = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers={"X-Consultation-Token": token},
        files={"file": ("examen.pdf", PDF_BYTES, "application/pdf")},
    )
    assert adjunto.status_code == 201, adjunto.text


async def test_estados_fuera_de_la_lista_blanca_dan_409(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA2.2/CA4.5: todo estado que no esté en la lista blanca rechaza con 409.

    Antes era una lista NEGRA (solo `waiting` y los cierres vencidos), así que `urgent_in_person`
    —y cualquier estado nuevo del enum— pasaba sin que nadie lo hubiera decidido. En
    `urgent_in_person` la vía es la atención presencial, no el seguimiento escrito.
    """
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)
    consulta = await db_session.get(Consultation, uuid.UUID(cid))
    assert consulta is not None

    for estado in ("urgent_in_person",):
        consulta.status = estado
        await db_session.flush()

        del_medico = await anon_client.post(
            f"{PREFIX}/consultations/{cid}/messages",
            headers=auth_headers(doc.id),
            json={"body": f"Mensaje con la consulta en {estado}"},
        )
        assert del_medico.status_code == 409, f"{estado}: {del_medico.text}"
        assert del_medico.json()["detail"] == "Esta consulta ya no admite mensajes."

        del_paciente = await anon_client.post(
            f"{PREFIX}/consultations/{cid}/messages",
            headers={"X-Consultation-Token": token},
            json={"body": f"Mensaje del paciente en {estado}"},
        )
        assert del_paciente.status_code == 409, f"{estado}: {del_paciente.text}"
        assert del_paciente.json()["detail"] == "Esta consulta ya no admite mensajes."

        # Tampoco se pueden subir adjuntos a un hilo que no admite mensajes
        adjunto = await anon_client.post(
            f"{PREFIX}/consultations/{cid}/attachments",
            headers=auth_headers(doc.id),
            files={"file": ("informe.pdf", PDF_BYTES, "application/pdf")},
        )
        assert adjunto.status_code == 409, f"{estado}: {adjunto.text}"


async def test_waiting_lo_escribe_el_paciente_pero_no_el_medico(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """En `waiting` aún no hay tratante: el médico recibe 409, el paciente puede escribir."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc, status="waiting")

    del_medico = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Mensaje del médico en espera"},
    )
    assert del_medico.status_code == 409
    assert "aún no ha sido tomada" in del_medico.json()["detail"]

    del_paciente = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Sigo esperando, doctora"},
    )
    assert del_paciente.status_code == 201, del_paciente.text


async def test_cerrada_dentro_de_la_ventana_sigue_admitiendo_mensajes(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Cerrada hace menos de MESSAGING_AFTER_CLOSE_HOURS: el hilo sigue abierto (CA2.2)."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)
    consulta = await db_session.get(Consultation, uuid.UUID(cid))
    assert consulta is not None
    consulta.status = "closed"
    consulta.closed_at = datetime.now(UTC) - timedelta(
        hours=settings.MESSAGING_AFTER_CLOSE_HOURS - 1
    )
    await db_session.flush()

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Seguimiento tras el cierre"},
    )
    assert resp.status_code == 201, resp.text

    # Sin ninguna fecha de referencia se cierra el paso (fail-closed). En la base `created_at`
    # es NOT NULL, así que la rama se prueba sobre un objeto suelto.
    huerfana = Consultation(status="cancelled")
    with pytest.raises(ConflictError):
        messaging.check_can_write_in_consultation(huerfana, is_doctor=True)

    # Vencida (cerrada hace más de la ventana): 409 también para el paciente
    consulta.status = "closed"
    consulta.closed_at = datetime.now(UTC) - timedelta(
        hours=settings.MESSAGING_AFTER_CLOSE_HOURS + 1
    )
    await db_session.flush()
    vencida = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Mensaje fuera de la ventana"},
    )
    assert vencida.status_code == 409
    assert vencida.json()["detail"] == "Esta consulta ya no admite mensajes."


# =====================================================================
# 10. El buzón del admin NO es el buzón de la plataforma (CA7.1)
# =====================================================================


async def test_inbox_del_admin_filtra_por_pertenencia(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    admin_identity: Profile,
    factory_de_prueba: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CA7.1: el admin, con `messages.read`, solo ve los hilos en los que es o fue tratante.

    El `/inbox` devolvía el buzón de toda la plataforma al admin, con la presencia del paciente
    incluida. Para supervisar tiene `consultations.read` y los metadatos del caso (CA3.2).
    """
    monkeypatch.setattr(settings, "WAITING_ROOM_STREAM_MAX_SECONDS", 0)
    doc = await add_doctor(db_session)
    cid_ajeno, _, token_ajeno = await _create_test_case(client, db_session, doctor=doc)
    await anon_client.post(
        f"{PREFIX}/consultations/{cid_ajeno}/messages",
        headers={"X-Consultation-Token": token_ajeno},
        json={"body": "Hilo de otro médico"},
    )

    # 1. El admin no es ni fue tratante de ese hilo: buzón vacío
    vacio = await client.get(f"{PREFIX}/inbox")
    assert vacio.status_code == 200
    assert vacio.json() == []

    # 2. Su stream tampoco cuenta ese hilo ni lo nombra
    stream = await client.get(f"{PREFIX}/inbox/stream")
    assert stream.status_code == 200
    assert '"unread_total":0' in stream.text
    assert cid_ajeno not in stream.text

    # 3. En cambio, el hilo de un caso que SÍ tiene asignado sí aparece
    cid_propio, _, token_propio = await _create_test_case(client, db_session, doctor=doc)
    consulta = await db_session.get(Consultation, uuid.UUID(cid_propio))
    assert consulta is not None
    consulta.assigned_doctor_id = admin_identity.id
    await db_session.flush()
    await anon_client.post(
        f"{PREFIX}/consultations/{cid_propio}/messages",
        headers={"X-Consultation-Token": token_propio},
        json={"body": "Hilo del propio admin"},
    )

    propio = await client.get(f"{PREFIX}/inbox")
    assert propio.status_code == 200
    hilos = propio.json()
    assert [h["consultation_id"] for h in hilos] == [cid_propio]
    assert hilos[0]["unread_count"] == 1


async def test_inbox_del_medico_filtra_hilos_ajenos_y_only_unread(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El buzón de un médico no incluye hilos de otro, y `only_unread` filtra los leídos."""
    doc_a = await add_doctor(db_session)
    doc_b = await add_doctor(db_session)
    cid_a, _, token_a = await _create_test_case(client, db_session, doctor=doc_a)
    cid_b, _, token_b = await _create_test_case(client, db_session, doctor=doc_b)

    for cid, token in ((cid_a, token_a), (cid_b, token_b)):
        await anon_client.post(
            f"{PREFIX}/consultations/{cid}/messages",
            headers={"X-Consultation-Token": token},
            json={"body": "Buenas tardes"},
        )

    inbox_a = await anon_client.get(f"{PREFIX}/inbox", headers=auth_headers(doc_a.id))
    assert [h["consultation_id"] for h in inbox_a.json()] == [cid_a]

    # Tras marcar leído, `only_unread=true` deja el buzón vacío
    await anon_client.post(
        f"{PREFIX}/consultations/{cid_a}/messages/read", headers=auth_headers(doc_a.id)
    )
    sin_leer = await anon_client.get(
        f"{PREFIX}/inbox?only_unread=true", headers=auth_headers(doc_a.id)
    )
    assert sin_leer.json() == []
    con_todos = await anon_client.get(f"{PREFIX}/inbox", headers=auth_headers(doc_a.id))
    assert len(con_todos.json()) == 1
    assert con_todos.json()[0]["last_direction"] == "patient_to_doctor"


# =====================================================================
# 11. Rate limit por IP en las escrituras (CA4.4)
# =====================================================================


async def test_rate_limit_por_ip_en_envio_y_en_adjuntos(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CA4.4: `PUBLIC_WRITE_RATE_LIMIT` por IP, ADEMÁS del tope horario por hilo.

    El tope por hilo no frena a quien crea casos nuevos para seguir escribiendo, y el
    endpoint lo sirve un paciente anónimo con solo un token de sala.
    """
    monkeypatch.setattr(limiter, "enabled", True)  # el conftest lo apaga para el resto
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    codigos = []
    for i in range(14):
        r = await anon_client.post(
            f"{PREFIX}/consultations/{cid}/messages",
            headers={"X-Consultation-Token": token},
            json={"body": f"Mensaje en ráfaga {i}"},
        )
        codigos.append(r.status_code)
    assert 429 in codigos, f"sin rate limit por IP: {codigos}"

    adjuntos = []
    for _ in range(14):
        r = await anon_client.post(
            f"{PREFIX}/consultations/{cid}/attachments",
            headers={"X-Consultation-Token": token},
            files={"file": ("examen.pdf", PDF_BYTES, "application/pdf")},
        )
        adjuntos.append(r.status_code)
    assert 429 in adjuntos, f"sin rate limit por IP en adjuntos: {adjuntos}"


# =====================================================================
# 12. Tope de caracteres del cuerpo desde Settings (CA2.3)
# =====================================================================


async def test_tope_de_caracteres_del_cuerpo(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA2.3: 0 a 2000 caracteres, y el tope sale de `MESSAGING_MAX_BODY_CHARS`."""
    assert settings.MESSAGING_MAX_BODY_CHARS == 2000, "CA2.3 fija el tope en 2000"
    limite = settings.MESSAGING_MAX_BODY_CHARS

    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)

    justo = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "a" * limite},
    )
    assert justo.status_code == 201, justo.text
    assert len(justo.json()["body"]) == limite

    excede = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "a" * (limite + 1)},
    )
    assert excede.status_code == 422

    # El OpenAPI publica el MISMO tope: el frontend lo usa como `maxLength` del textarea
    esquema = (await anon_client.get(f"{PREFIX}/openapi.json")).json()
    body_schema = esquema["components"]["schemas"]["MessageCreate"]["properties"]["body"]
    assert any(v.get("maxLength") == limite for v in body_schema["anyOf"])


# =====================================================================
# 13. Concurrencia (R5 doble marcado, y doble envío con el mismo client_msg_id)
# =====================================================================
#
# Usan sesiones y conexiones REALES (sin el override de `get_db`): con la sesión compartida del
# resto de los tests no habría dos transacciones y la carrera no probaría nada. Mismo montaje
# que `tests/test_queue_concurrency.py`.


async def _sembrar_hilo_real() -> dict:
    """Committea un médico con ficha habilitada, su paciente y una consulta `in_progress`."""
    async with AsyncSessionLocal() as s:
        doctor = make_profile(role="doctor", specialty=GENERAL)
        doctor.specialty_id = await specialty_id_by_name(s, GENERAL)
        s.add(doctor)
        await s.flush()
        s.add(make_doctor_row(doctor.id))

        paciente = Patient(
            full_name="Paciente Mensajeria Concurrente",
            phone_whatsapp="+58412999888",
            affected_zone="Caracas",
            consent=True,
        )
        s.add(paciente)
        await s.flush()

        consulta = Consultation(
            code=f"TEST-{uuid.uuid4().hex[:10]}",
            patient_id=paciente.id,
            specialty_id=doctor.specialty_id,
            status="in_progress",
            assigned_doctor_id=doctor.id,
        )
        s.add(consulta)
        await s.commit()
        return {
            "consultation_id": consulta.id,
            "patient_id": paciente.id,
            "doctor_id": doctor.id,
        }


async def _borrar_hilo_real(datos: dict) -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Message).where(Message.consultation_id == datos["consultation_id"]))
        await s.execute(delete(Consultation).where(Consultation.id == datos["consultation_id"]))
        await s.execute(delete(Patient).where(Patient.id == datos["patient_id"]))
        await s.execute(delete(Doctor).where(Doctor.user_id == datos["doctor_id"]))
        await s.execute(delete(Profile).where(Profile.id == datos["doctor_id"]))
        await s.commit()


@pytest_asyncio.fixture
async def hilo_real() -> AsyncGenerator[dict, None]:
    datos = await _sembrar_hilo_real()
    try:
        yield datos
    finally:
        await _borrar_hilo_real(datos)


async def test_envios_simultaneos_con_el_mismo_client_msg_id(
    live_client: AsyncClient, hilo_real: dict
) -> None:
    """Dos envíos a la vez con el mismo `client_msg_id` dejan UNA fila, nunca un 500.

    La idempotencia se resolvía con un SELECT previo, que no cierra la carrera: ambos pasaban
    el chequeo y el segundo INSERT choca con el índice único parcial
    `uq_messages_consultation_client_msg`. Ahora ese choque se captura, se vuelve atrás el
    savepoint y se devuelve el mensaje que ganó.
    """
    cid = hilo_real["consultation_id"]
    headers = auth_headers(hilo_real["doctor_id"])
    client_msg_id = f"carrera-{uuid.uuid4()}"

    async def enviar() -> httpx.Response:
        return await live_client.post(
            f"{PREFIX}/consultations/{cid}/messages",
            headers=headers,
            json={"body": "Mensaje duplicado por doble clic", "client_msg_id": client_msg_id},
        )

    respuestas = await asyncio.gather(*[enviar() for _ in range(4)])
    codigos = [r.status_code for r in respuestas]
    assert all(c in (200, 201) for c in codigos), codigos
    assert codigos.count(201) == 1, f"debe crearse exactamente una vez: {codigos}"
    assert len({r.json()["id"] for r in respuestas}) == 1, "todos devuelven el mismo mensaje"

    async with AsyncSessionLocal() as s:
        filas = await s.scalar(
            select(func.count(Message.id)).where(
                Message.consultation_id == cid, Message.client_msg_id == client_msg_id
            )
        )
    assert filas == 1


async def test_doble_marcado_leido_concurrente_es_idempotente(
    live_client: AsyncClient, hilo_real: dict
) -> None:
    """R5/CA5.1: N marcados simultáneos reparten el conteo; nada se marca dos veces.

    La escritura es condicional (`UPDATE ... WHERE read_at IS NULL`) y lo que se devuelve es el
    `rowcount`: si dos marcados contaran los mismos mensajes, la suma pasaría del total y el
    buzón del médico restaría no leídos que no existen.
    """
    cid = hilo_real["consultation_id"]
    headers = auth_headers(hilo_real["doctor_id"])

    total_mensajes = 3
    async with AsyncSessionLocal() as s:
        for i in range(total_mensajes):
            s.add(
                Message(
                    id=uuid.uuid4(),
                    consultation_id=cid,
                    sender_role="patient",
                    direction="patient_to_doctor",
                    channel="web",
                    kind="text",
                    body=f"Mensaje sin leer {i}",
                    sent_at=datetime.now(UTC),
                    delivery_status="sent",
                )
            )
        await s.commit()

    async def marcar() -> int:
        resp = await live_client.post(
            f"{PREFIX}/consultations/{cid}/messages/read", headers=headers
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["marked"]

    marcados = await asyncio.gather(*[marcar() for _ in range(4)])
    assert sum(marcados) == total_mensajes, f"doble marcado: {marcados}"
    assert max(marcados) <= total_mensajes

    async with AsyncSessionLocal() as s:
        pendientes = await s.scalar(
            select(func.count(Message.id)).where(
                Message.consultation_id == cid,
                Message.direction == "patient_to_doctor",
                Message.read_at.is_(None),
            )
        )
    assert pendientes == 0

    # Y un marcado posterior sigue siendo un no-op (idempotencia)
    assert await marcar() == 0


# =====================================================================
# 14. Cliente de Supabase Storage (bucket privado, CA15.3)
# =====================================================================


async def test_storage_guarda_lee_y_borra_en_el_bucket(bucket_falso: dict[str, bytes]) -> None:
    """El binario va al bucket por su ruta de UUID; borrar dos veces no es un error."""
    ruta = f"consultations/{uuid.uuid4()}/attachments/{uuid.uuid4()}.bin"

    assert await storage.save_attachment_file(ruta, PDF_BYTES, "application/pdf") == ruta
    assert bucket_falso[ruta] == PDF_BYTES
    assert await storage.get_attachment_file(ruta) == PDF_BYTES
    assert await storage.delete_attachment_file(ruta) is True
    assert await storage.delete_attachment_file(ruta) is False
    assert await storage.get_attachment_file(ruta) is None


async def test_storage_rechaza_rutas_de_traversal() -> None:
    """Una ruta con `..` o absoluta no puede salirse del prefijo del bucket."""
    for ruta in ("../../etc/passwd", "/", "..", ""):
        with pytest.raises(UpstreamServiceError):
            await storage.get_attachment_file(ruta)


async def test_storage_traduce_fallos_a_error_de_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage caído o rechazando no se escapa como 500: es un 502 sin detalles internos."""

    def error_del_servidor(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "InternalError", "bucket": "chat-attachments"})

    monkeypatch.setattr(storage, "_transport", httpx.MockTransport(error_del_servidor))
    ruta = "consultations/x/attachments/y.bin"
    with pytest.raises(UpstreamServiceError):
        await storage.save_attachment_file(ruta, PDF_BYTES, "application/pdf")
    with pytest.raises(UpstreamServiceError):
        await storage.get_attachment_file(ruta)
    with pytest.raises(UpstreamServiceError):
        await storage.delete_attachment_file(ruta)

    def sin_red(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red", request=request)

    monkeypatch.setattr(storage, "_transport", httpx.MockTransport(sin_red))
    with pytest.raises(UpstreamServiceError):
        await storage.save_attachment_file(ruta, PDF_BYTES, "application/pdf")
    with pytest.raises(UpstreamServiceError):
        await storage.get_attachment_file(ruta)
    with pytest.raises(UpstreamServiceError):
        await storage.delete_attachment_file(ruta)


# =====================================================================
# 15. Presencia y validaciones (unidad)
# =====================================================================


def test_presencia_del_paciente_usa_el_umbral_de_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El umbral sale de `MESSAGING_PATIENT_PRESENCE_TTL_SECONDS`, no de un 45 cableado."""
    monkeypatch.setattr(settings, "MESSAGING_PATIENT_PRESENCE_TTL_SECONDS", 45)
    cid = uuid.uuid4()
    assert messaging.is_patient_online(cid) is False

    messaging.record_patient_presence(cid)
    assert messaging.is_patient_online(cid) is True

    # Una señal más vieja que el umbral se descarta y se limpia del registro en memoria
    messaging._ACTIVE_PATIENTS[cid] = datetime.now(UTC) - timedelta(seconds=46)
    assert messaging.is_patient_online(cid) is False
    assert cid not in messaging._ACTIVE_PATIENTS

    # Respaldo persistente (`consultations.patient_last_seen_at`), con y sin zona
    reciente = datetime.now(UTC) - timedelta(seconds=5)
    assert messaging.is_patient_online(cid, reciente) is True
    assert messaging.is_patient_online(cid, reciente.replace(tzinfo=None)) is True
    assert messaging.is_patient_online(cid, datetime.now(UTC) - timedelta(seconds=120)) is False

    monkeypatch.setattr(settings, "MESSAGING_PATIENT_PRESENCE_TTL_SECONDS", 1)
    assert messaging.is_patient_online(cid, reciente) is False


def test_validacion_de_adjuntos_nombre_y_tamano() -> None:
    """Nombre vacío, nombre en blanco y archivo por encima del tope: 422."""
    with pytest.raises(UnprocessableError):
        messaging.validate_attachment_file("", PDF_BYTES, "application/pdf")
    with pytest.raises(UnprocessableError):
        messaging.validate_attachment_file("   ", PDF_BYTES, "application/pdf")

    enorme = b"%PDF-1.4" + b"0" * settings.MESSAGING_MAX_ATTACHMENT_SIZE_BYTES
    with pytest.raises(UnprocessableError) as exc:
        messaging.validate_attachment_file("enorme.pdf", enorme, "application/pdf")
    assert "tamaño máximo" in str(exc.value)


def test_ventana_tras_cierre_con_fecha_sin_zona() -> None:
    """Una `closed_at` sin zona se interpreta como UTC, no revienta la comparación."""
    reciente = Consultation(status="closed", closed_at=datetime.now(UTC).replace(tzinfo=None))
    messaging.check_can_write_in_consultation(reciente, is_doctor=True)

    vencida = Consultation(
        status="closed",
        closed_at=(
            datetime.now(UTC) - timedelta(hours=settings.MESSAGING_AFTER_CLOSE_HOURS + 1)
        ).replace(tzinfo=None),
    )
    with pytest.raises(ConflictError):
        messaging.check_can_write_in_consultation(vencida, is_doctor=True)


# =====================================================================
# 16. Pertenencia: 404 en TODAS las rutas del hilo (IDOR)
# =====================================================================


async def test_consulta_inexistente_da_404_en_todas_las_rutas(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Ninguna ruta del hilo filtra la existencia de una consulta que no está."""
    doc = await add_doctor(db_session)
    h = auth_headers(doc.id)
    fantasma = uuid.uuid4()

    assert (
        await anon_client.get(f"{PREFIX}/consultations/{fantasma}/messages", headers=h)
    ).status_code == 404
    assert (
        await anon_client.post(
            f"{PREFIX}/consultations/{fantasma}/messages", headers=h, json={"body": "Hola"}
        )
    ).status_code == 404
    assert (
        await anon_client.post(f"{PREFIX}/consultations/{fantasma}/messages/read", headers=h)
    ).status_code == 404
    assert (
        await anon_client.post(
            f"{PREFIX}/consultations/{fantasma}/attachments",
            headers=h,
            files={"file": ("informe.pdf", PDF_BYTES, "application/pdf")},
        )
    ).status_code == 404
    assert (
        await anon_client.get(
            f"{PREFIX}/consultations/{fantasma}/attachments/{uuid.uuid4()}", headers=h
        )
    ).status_code == 404


async def test_medico_ajeno_y_token_cruzado_dan_404_en_todo_el_hilo(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Ni un médico de otro caso ni un token de otra sala leen, marcan ni suben adjuntos."""
    doc = await add_doctor(db_session)
    ajeno = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)
    _, _, token_de_otro = await _create_test_case(client, db_session, doctor=doc)

    for headers in (auth_headers(ajeno.id), {"X-Consultation-Token": token_de_otro}):
        assert (
            await anon_client.get(f"{PREFIX}/consultations/{cid}/messages", headers=headers)
        ).status_code == 404
        assert (
            await anon_client.post(f"{PREFIX}/consultations/{cid}/messages/read", headers=headers)
        ).status_code == 404
        assert (
            await anon_client.post(
                f"{PREFIX}/consultations/{cid}/attachments",
                headers=headers,
                files={"file": ("informe.pdf", PDF_BYTES, "application/pdf")},
            )
        ).status_code == 404
        assert (
            await anon_client.get(
                f"{PREFIX}/consultations/{cid}/attachments/{uuid.uuid4()}", headers=headers
            )
        ).status_code == 404


async def test_el_admin_no_marca_como_leido(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El admin no es parte de la conversación: marcar leído es 403, no un 200 silencioso."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Mensaje del paciente"},
    )

    resp = await client.post(f"{PREFIX}/consultations/{cid}/messages/read")
    assert resp.status_code == 403


async def test_el_paciente_marca_como_leido_lo_del_medico(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El paciente marca leído solo la dirección contraria (CA5.1)."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Le escribo el resultado"},
    )
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Gracias doctora"},
    )

    marcado = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages/read",
        headers={"X-Consultation-Token": token},
    )
    assert marcado.status_code == 200
    assert marcado.json()["marked"] == 1  # solo el del médico

    # Y el médico sigue teniendo el suyo sin leer
    hilo = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages", headers=auth_headers(doc.id)
    )
    assert hilo.json()["unread_count"] == 1


async def test_adjunto_de_otra_consulta_no_se_puede_vincular(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Vincular un adjunto que no es de este hilo es 400, no un mensaje a medias."""
    doc = await add_doctor(db_session)
    cid_a, _, _ = await _create_test_case(client, db_session, doctor=doc)
    cid_b, _, _ = await _create_test_case(client, db_session, doctor=doc)

    subido = await anon_client.post(
        f"{PREFIX}/consultations/{cid_b}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("informe.pdf", PDF_BYTES, "application/pdf")},
    )
    att_id = subido.json()["id"]

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid_a}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Mira esto", "attachment_ids": [att_id]},
    )
    assert resp.status_code == 400

    inexistente = await anon_client.post(
        f"{PREFIX}/consultations/{cid_a}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Mira esto", "attachment_ids": [str(uuid.uuid4())]},
    )
    assert inexistente.status_code == 400


async def test_adjunto_sin_binario_en_el_bucket_da_404(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Si la fila existe pero el objeto no está en el bucket, es 404 y no un 500."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)
    subido = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("informe.pdf", PDF_BYTES, "application/pdf")},
    )
    att_id = subido.json()["id"]

    ruta = f"consultations/{cid}/attachments/{att_id}.bin"
    assert await storage.delete_attachment_file(ruta) is True

    resp = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/attachments/{att_id}", headers=auth_headers(doc.id)
    )
    assert resp.status_code == 404


async def test_paginacion_del_hilo_con_after_id_y_before_id(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """`after_id` y `before_id` recortan el hilo por `sent_at, id` (orden estable)."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    ids = []
    for i in range(3):
        resp = await anon_client.post(
            f"{PREFIX}/consultations/{cid}/messages",
            headers={"X-Consultation-Token": token},
            json={"body": f"Mensaje {i}"},
        )
        ids.append(resp.json()["id"])

    h = auth_headers(doc.id)
    despues = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages?after_id={ids[0]}", headers=h
    )
    assert [m["id"] for m in despues.json()["items"]] == ids[1:]

    antes = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages?before_id={ids[2]}", headers=h
    )
    assert [m["id"] for m in antes.json()["items"]] == ids[:2]

    # Un id inexistente no recorta nada (no es un filtro silencioso ni un error)
    todos = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages?after_id={uuid.uuid4()}", headers=h
    )
    assert [m["id"] for m in todos.json()["items"]] == ids


async def test_hilo_de_la_cadena_de_derivacion(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA1.3/CA2.1: quien FUE tratante en la cadena sigue leyendo el hilo de la consulta hija."""
    doc_padre = await add_doctor(db_session)
    doc_hija = await add_doctor(db_session)
    cid_padre, patient_id, _ = await _create_test_case(client, db_session, doctor=doc_padre)

    padre = await db_session.get(Consultation, uuid.UUID(cid_padre))
    assert padre is not None

    cid_hija, _, token_hija = await _create_test_case(client, db_session, doctor=doc_hija)
    hija = await db_session.get(Consultation, uuid.UUID(cid_hija))
    assert hija is not None
    hija.parent_consultation_id = padre.id
    await db_session.flush()

    await anon_client.post(
        f"{PREFIX}/consultations/{cid_hija}/messages",
        headers={"X-Consultation-Token": token_hija},
        json={"body": "Pregunta para el especialista"},
    )

    # El médico de la consulta PADRE lee y escribe en el hilo de la hija
    lectura = await anon_client.get(
        f"{PREFIX}/consultations/{cid_hija}/messages", headers=auth_headers(doc_padre.id)
    )
    assert lectura.status_code == 200
    assert lectura.json()["items"][0]["body"] == "Pregunta para el especialista"

    # Y ese hilo aparece en su buzón, aunque la hija esté asignada a otro médico
    inbox = await anon_client.get(f"{PREFIX}/inbox", headers=auth_headers(doc_padre.id))
    assert cid_hija in [h["consultation_id"] for h in inbox.json()]


# =====================================================================
# 17. Guardas del router (401 / 403 / multipart) y SSE del buzón
# =====================================================================


@pytest.fixture
def staff_sin_permisos_de_mensajeria() -> Iterator[Principal]:
    """Staff (admin) SIN `messages.read`/`messages.write`: las rutas deben dar 403.

    Se inyecta el `Principal` porque todos los roles sembrados traen los dos permisos (ver la
    migración de RBAC), así que con un usuario real no se puede llegar a este estado — y la
    guarda del router es justo lo que protege al módulo si un día se siembra otro rol.
    """
    principal = Principal(
        id=uuid.uuid4(),
        role="admin",
        active=True,
        verified=True,
        roles=frozenset({"admin"}),
        permissions=frozenset(),
    )
    app.dependency_overrides[get_optional_principal] = lambda: principal
    yield principal
    app.dependency_overrides.pop(get_optional_principal, None)


async def test_sin_credenciales_todas_las_rutas_del_hilo_dan_401(
    anon_client: AsyncClient,
) -> None:
    """Sin sesión ni token de sala no se entra al hilo (ni para leer metadatos)."""
    cid = uuid.uuid4()
    assert (await anon_client.get(f"{PREFIX}/consultations/{cid}/messages")).status_code == 401
    assert (
        await anon_client.post(f"{PREFIX}/consultations/{cid}/messages", json={"body": "Hola"})
    ).status_code == 401
    assert (
        await anon_client.post(f"{PREFIX}/consultations/{cid}/messages/read")
    ).status_code == 401
    assert (
        await anon_client.post(
            f"{PREFIX}/consultations/{cid}/attachments",
            files={"file": ("informe.pdf", PDF_BYTES, "application/pdf")},
        )
    ).status_code == 401
    assert (
        await anon_client.get(f"{PREFIX}/consultations/{cid}/attachments/{uuid.uuid4()}")
    ).status_code == 401
    assert (await anon_client.post(f"{PREFIX}/consultations/{cid}/video-call")).status_code == 401


async def test_staff_sin_permiso_de_mensajeria_recibe_403(
    anon_client: AsyncClient, staff_sin_permisos_de_mensajeria: Principal
) -> None:
    """Un miembro del staff sin `messages.*` no pasa, aunque tenga sesión válida."""
    cid = uuid.uuid4()
    assert (await anon_client.get(f"{PREFIX}/consultations/{cid}/messages")).status_code == 403
    assert (
        await anon_client.post(f"{PREFIX}/consultations/{cid}/messages", json={"body": "Hola"})
    ).status_code == 403
    assert (
        await anon_client.post(f"{PREFIX}/consultations/{cid}/messages/read")
    ).status_code == 403
    assert (
        await anon_client.post(
            f"{PREFIX}/consultations/{cid}/attachments",
            files={"file": ("informe.pdf", PDF_BYTES, "application/pdf")},
        )
    ).status_code == 403
    assert (
        await anon_client.get(f"{PREFIX}/consultations/{cid}/attachments/{uuid.uuid4()}")
    ).status_code == 403
    assert (await anon_client.post(f"{PREFIX}/consultations/{cid}/video-call")).status_code == 403


async def test_subida_sin_multipart_valido_da_422(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """La subida exige `multipart/form-data` con una parte de archivo de verdad."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)
    url = f"{PREFIX}/consultations/{cid}/attachments"
    h = auth_headers(doc.id)

    no_multipart = await anon_client.post(url, headers=h, json={"file": "informe.pdf"})
    assert no_multipart.status_code == 422
    assert "multipart" in no_multipart.json()["detail"]

    vacio = await anon_client.post(
        url,
        headers={**h, "Content-Type": "multipart/form-data; boundary=frontera"},
        content=b"",
    )
    assert vacio.status_code == 422

    sin_archivo = await anon_client.post(
        url,
        headers={**h, "Content-Type": "multipart/form-data; boundary=frontera"},
        content=(
            b"--frontera\r\n"
            b'Content-Disposition: form-data; name="campo"\r\n\r\n'
            b"valor\r\n"
            b"--frontera--\r\n"
        ),
    )
    assert sin_archivo.status_code == 422
    assert "archivo" in sin_archivo.json()["detail"]


async def test_inbox_stream_anuncia_solo_los_hilos_que_cambiaron(
    client: AsyncClient,
    db_session: AsyncSession,
    factory_de_prueba: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8.2: tras el primer evento, el stream nombra solo los hilos que cambiaron, y late."""
    estable, nuevo = uuid.uuid4(), uuid.uuid4()
    estados = [
        (0, {estable: (None, 0)}),
        (1, {estable: (None, 0), nuevo: (None, 1)}),
    ]

    async def senal(*_: object, **__: object) -> tuple[int, dict]:
        return estados.pop(0) if estados else (1, {estable: (None, 0), nuevo: (None, 1)})

    monkeypatch.setattr(messaging, "get_inbox_signal", senal)
    monkeypatch.setattr(settings, "WAITING_ROOM_POLL_SECONDS", 0.01)
    monkeypatch.setattr(settings, "WAITING_ROOM_HEARTBEAT_SECONDS", 0)
    monkeypatch.setattr(settings, "WAITING_ROOM_STREAM_MAX_SECONDS", 0.1)

    resp = await client.get(f"{PREFIX}/inbox/stream")
    assert resp.status_code == 200
    assert ": ping" in resp.text  # latido

    eventos = [linea for linea in resp.text.splitlines() if linea.startswith("data:")]
    assert len(eventos) >= 2, resp.text
    assert str(nuevo) in eventos[1]
    assert str(estable) not in eventos[1]


async def test_inbox_stream_se_cierra_si_falla_la_senal(
    client: AsyncClient,
    db_session: AsyncSession,
    factory_de_prueba: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un fallo leyendo la señal cierra el stream sin filtrar el error al cliente."""

    async def explota(*_: object, **__: object) -> tuple[int, dict]:
        raise RuntimeError("la base se cayó")

    monkeypatch.setattr(messaging, "get_inbox_signal", explota)
    monkeypatch.setattr(settings, "WAITING_ROOM_STREAM_MAX_SECONDS", 0.1)

    resp = await client.get(f"{PREFIX}/inbox/stream")
    assert resp.status_code == 200
    assert "la base se cayó" not in resp.text
    assert "event: inbox" not in resp.text


async def test_medico_reasignado_conserva_el_hilo_del_tramo_que_atendio(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA3.1: quien TOMÓ el caso sigue leyendo y escribiendo aunque luego se lo reasignen.

    Es el daño colateral a evitar al estrechar el criterio del grant (backlog C-13): su
    asignación ya no está en la fila de la consulta, pero el evento `opened` que escribió el
    claim sí — y ése sí significa «atendí este caso». Se monta con los endpoints reales (claim
    atómico + reasignación del admin por PATCH), no insertando el evento a mano.
    """
    primero = await add_doctor(db_session, specialty=GENERAL)
    segundo = await add_doctor(db_session, specialty=GENERAL)
    cid, _, token = await _create_test_case(client, db_session, status="waiting")

    tomado = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(primero.id)
    )
    assert tomado.status_code == 200, tomado.text
    assert tomado.json()["assigned_doctor_id"] == str(primero.id)

    # El paciente le escribe sobre el tramo que él atendió
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Doctora, sigo con el dolor que le comenté"},
    )

    # Un admin reasigna el caso a otro médico
    reasignado = await client.patch(
        f"{PREFIX}/consultations/{cid}", json={"assigned_doctor_id": str(segundo.id)}
    )
    assert reasignado.status_code == 200, reasignado.text
    assert reasignado.json()["assigned_doctor_id"] == str(segundo.id)

    # `primero` ya no es el asignado y CONSERVA el hilo: lee el cuerpo y puede responder
    lectura = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages", headers=auth_headers(primero.id)
    )
    assert lectura.status_code == 200, lectura.text
    assert lectura.json()["clinical_access"] == "full"
    assert lectura.json()["items"][0]["body"] == "Doctora, sigo con el dolor que le comenté"

    respuesta = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(primero.id),
        json={"body": "Lo veo mañana a primera hora"},
    )
    assert respuesta.status_code == 201, respuesta.text


async def test_descarga_con_token_de_otra_sala_da_404(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El token de otra sala no descarga el adjunto de esta, ni revela que existe."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)
    _, _, token_de_otro = await _create_test_case(client, db_session, doctor=doc)

    subido = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("biopsia.pdf", PDF_BYTES, "application/pdf")},
    )
    att_id = subido.json()["id"]

    resp = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/attachments/{att_id}",
        headers={"X-Consultation-Token": token_de_otro},
    )
    assert resp.status_code == 404


# =====================================================================
# 9. Iniciar la videollamada desde el hilo (R16)
# =====================================================================


async def _system_call_messages(db: AsyncSession, cid: str) -> list[Message]:
    """Mensajes de sistema de llamada del hilo, en orden."""
    rows = await db.execute(
        select(Message)
        .where(
            Message.consultation_id == uuid.UUID(cid),
            Message.sender_role == "system",
        )
        .order_by(Message.sent_at.asc(), Message.id.asc())
    )
    return list(rows.scalars().all())


async def _call_started_entries(db: AsyncSession, cid: str) -> list[AuditLog]:
    """Trazas `call.started` de la consulta.

    Sin `order_by` por `created_at`: en los tests todo corre dentro de UNA transacción
    (savepoints), y el `now()` de Postgres es el de la transacción, así que las dos filas
    comparten marca de tiempo y el orden sería indefinido. Se comparan como conjunto.
    """
    rows = await db.execute(
        select(AuditLog).where(AuditLog.action == "call.started", AuditLog.resource_id == cid)
    )
    return list(rows.scalars().all())


async def test_el_medico_tratante_inicia_la_llamada_y_deja_un_mensaje_de_sistema(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA16.3/CA16.4/CA16.5/CA16.7: 201 con `{room_url, message_id}`, un único mensaje de
    sistema en el hilo y la traza `call.started` sin la URL de la sala."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()

    # Al médico sí se le devuelve la sala de Jitsi: es la ventana que abre con el clic.
    assert settings.JITSI_DOMAIN in data["room_url"]
    consulta = await db_session.get(Consultation, uuid.UUID(cid))
    assert consulta is not None
    assert consulta.video_room_url == data["room_url"]

    mensajes = await _system_call_messages(db_session, cid)
    assert len(mensajes) == 1
    sistema = mensajes[0]
    assert str(sistema.id) == data["message_id"]
    assert sistema.sender_role == "system"
    assert sistema.direction == "system"
    assert sistema.kind == "call"
    assert sistema.sender_user_id is None
    assert sistema.channel == "web"
    # La columna apunta a una tabla que no existe (deuda declarada): se queda nula.
    assert sistema.call_session_id is None

    # Cifrado en reposo: el CHECK `messages_body_cifrado` exige el prefijo `enc:v1:`.
    crudo = await db_session.scalar(
        text("select body from messages where id = :id"), {"id": sistema.id}
    )
    assert crudo.startswith("enc:v1:")

    # Auditoría: `call.started`, sin contenido y sin la URL de la sala.
    entradas = await _call_started_entries(db_session, cid)
    assert len(entradas) == 1
    traza = entradas[0]
    assert traza.actor_user_id == doc.id
    assert traza.resource == "consultations"
    assert settings.JITSI_DOMAIN not in json.dumps(traza.metadata_)
    assert "body" not in (traza.metadata_ or {})


async def test_la_reentrada_reutiliza_sala_y_aviso_pero_deja_su_propia_traza(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA16.2b/CA16.4: volver a entrar no duplica nada visible para el paciente.

    `ensure_video_room` es idempotente (dos clics no dejan a médico y paciente en salas
    distintas) y el aviso del hilo se **reutiliza** dentro de la ventana: el médico entra y sale
    de la sala varias veces en una misma atención, y un aviso por intento dejaría cinco líneas
    idénticas en el historial clínico.

    Lo que sí se repite es el `audit_log`: cada intento de llamada es una traza legítima
    (CA16.9). El hilo cuenta la conversación; el audit cuenta los intentos.
    """
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)

    primera = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    segunda = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert primera.status_code == 201 and segunda.status_code == 201
    assert primera.json()["room_url"] == segunda.json()["room_url"]
    # Mismo aviso: el contrato no cambia, se devuelve el `message_id` del que ya estaba.
    assert primera.json()["message_id"] == segunda.json()["message_id"]

    mensajes = await _system_call_messages(db_session, cid)
    assert len(mensajes) == 1
    # Y una sola sala en la consulta.
    consulta = await db_session.get(Consultation, uuid.UUID(cid))
    assert consulta is not None
    assert consulta.video_room_url == primera.json()["room_url"]

    # Dos intentos, dos trazas. La segunda dice que el aviso se reutilizó.
    trazas = await _call_started_entries(db_session, cid)
    assert len(trazas) == 2
    assert sorted(t.metadata_["notice_reused"] for t in trazas) == [False, True]


async def test_pasada_la_ventana_la_llamada_deja_un_aviso_nuevo(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Fuera de `MESSAGING_CALL_NOTICE_WINDOW_MINUTES` ya no es una reentrada: es una llamada
    nueva y el paciente necesita saberlo, así que lleva su propio aviso con su propia hora.

    Se envejece el aviso anterior en lugar de esperar media hora: la ventana es la de `Settings`.
    """
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)

    primera = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert primera.status_code == 201

    viejo = (await _system_call_messages(db_session, cid))[0]
    viejo.sent_at = datetime.now(UTC) - timedelta(
        minutes=settings.MESSAGING_CALL_NOTICE_WINDOW_MINUTES + 1
    )
    await db_session.flush()

    segunda = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert segunda.status_code == 201
    assert segunda.json()["message_id"] != primera.json()["message_id"]
    assert len(await _system_call_messages(db_session, cid)) == 2
    # La sala sigue siendo la misma: lo que caducó es el aviso, no la consulta.
    assert segunda.json()["room_url"] == primera.json()["room_url"]


async def test_la_sala_que_ya_existia_no_se_regenera(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Si la consulta ya traía sala (la crea el panel antes del claim), se devuelve esa misma."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)
    consulta = await db_session.get(Consultation, uuid.UUID(cid))
    assert consulta is not None
    sala_previa = f"https://{settings.JITSI_DOMAIN}/vamed-sala-previa"
    consulta.video_room_url = sala_previa
    await db_session.flush()

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["room_url"] == sala_previa


async def test_solo_el_medico_tratante_inicia_la_videollamada(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA16.3: médico ajeno, admin no tratante y paciente (sesión o token) reciben 404.

    El 404 y no 403 es deliberado: un 403 confirmaría que esa consulta existe. Y el paciente
    con token válido es la barrera de la asimetría — el token abre SU hilo, no la llamada.
    """
    doc = await add_doctor(db_session)
    ajeno = await add_doctor(db_session)
    paciente_user_id = uuid.uuid4()
    db_session.add(
        Profile(
            id=paciente_user_id,
            full_name="Paciente Con Cuenta",
            role="patient",
            active=True,
            verified=True,
            role_chosen=True,
        )
    )
    await db_session.flush()
    cid, _, token = await _create_test_case(
        client, db_session, doctor=doc, patient_user_id=paciente_user_id
    )

    # 1. Médico de otro caso
    r_ajeno = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(ajeno.id)
    )
    assert r_ajeno.status_code == 404, r_ajeno.text

    # 2. Paciente con token válido de ESA consulta (test obligatorio de la asimetría)
    r_token = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers={"X-Consultation-Token": token}
    )
    assert r_token.status_code == 404, r_token.text

    # 3. Paciente con su propia sesión
    r_sesion = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(paciente_user_id)
    )
    assert r_sesion.status_code == 404, r_sesion.text

    # 4. Admin no tratante (tiene `messages.write`, pero no es el médico del caso)
    r_admin = await client.post(f"{PREFIX}/consultations/{cid}/video-call")
    assert r_admin.status_code == 404, r_admin.text

    # 5. Consulta inexistente
    r_fantasma = await anon_client.post(
        f"{PREFIX}/consultations/{uuid.uuid4()}/video-call", headers=auth_headers(doc.id)
    )
    assert r_fantasma.status_code == 404

    # Ninguno de los rechazos dejó sala ni mensaje en el hilo.
    consulta = await db_session.get(Consultation, uuid.UUID(cid))
    assert consulta is not None
    assert consulta.video_room_url is None
    assert await _system_call_messages(db_session, cid) == []


async def test_el_medico_que_intervino_antes_en_la_cadena_puede_iniciar(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA16.3: «tratante actual o previo en la cadena», el mismo criterio del resto del hilo."""
    primero = await add_doctor(db_session)
    especialista = await add_doctor(db_session)
    # La cadena real: el caso que atendió `primero` quedó como PADRE de la consulta del
    # especialista. No basta con que `primero` haya dejado un evento en la hija — eso lo deja
    # también un admin que la gestiona (backlog C-13).
    cid_padre, _, _ = await _create_test_case(client, db_session, doctor=primero)
    cid, _, _ = await _create_test_case(client, db_session, doctor=especialista)
    consulta = await db_session.get(Consultation, uuid.UUID(cid))
    assert consulta is not None
    consulta.parent_consultation_id = uuid.UUID(cid_padre)
    await db_session.flush()

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(primero.id)
    )
    assert resp.status_code == 201, resp.text


async def test_videollamada_en_consulta_que_no_admite_mensajes_da_409(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA16.4: fuera de la lista blanca de `check_can_write_in_consultation`, 409.

    `referred_to_specialist` sí admite mensajes pero ya no admite sala (`_ROOM_STATUSES` de
    `services/consultations.py`): también 409, el que lanza `ensure_video_room`.
    """
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)
    consulta = await db_session.get(Consultation, uuid.UUID(cid))
    assert consulta is not None

    consulta.status = "urgent_in_person"
    await db_session.flush()
    fuera = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert fuera.status_code == 409, fuera.text
    assert fuera.json()["detail"] == "Esta consulta ya no admite mensajes."

    consulta.status = "waiting"
    await db_session.flush()
    en_espera = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert en_espera.status_code == 409, en_espera.text

    consulta.status = "referred_to_specialist"
    await db_session.flush()
    derivada = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert derivada.status_code == 409, derivada.text
    assert derivada.json()["detail"] == "La consulta ya no está abierta."

    assert await _system_call_messages(db_session, cid) == []


async def test_el_aviso_de_la_llamada_se_lee_con_grant_y_sale_null_sin_el(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El cuerpo del aviso es contenido clínico como cualquier otro: lo ven el médico tratante
    y el paciente dueño (`summary_grant`), y sale `null` para quien no tiene grant (el admin).
    """
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    inicio = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert inicio.status_code == 201, inicio.text
    message_id = inicio.json()["message_id"]

    def _aviso(payload: dict) -> dict:
        avisos = [m for m in payload["items"] if m["id"] == message_id]
        assert len(avisos) == 1
        return avisos[0]

    # 1. El médico tratante lo lee descifrado
    del_medico = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages", headers=auth_headers(doc.id)
    )
    assert del_medico.status_code == 200
    cuerpo_medico = _aviso(del_medico.json())["body"]
    assert cuerpo_medico is not None

    # 2. El paciente dueño también: este mensaje existe para que se entere
    del_paciente = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages", headers={"X-Consultation-Token": token}
    )
    assert del_paciente.status_code == 200
    aviso_paciente = _aviso(del_paciente.json())
    assert aviso_paciente["body"] == cuerpo_medico
    assert aviso_paciente["kind"] == "call"
    assert aviso_paciente["direction"] == "system"
    assert aviso_paciente["sender_role"] == "system"

    # 3. Ni la sala de Jitsi ni ninguna otra URL se cuelan en el cuerpo
    assert settings.JITSI_DOMAIN not in cuerpo_medico
    assert inicio.json()["room_url"] not in cuerpo_medico

    # 4. Sin grant (admin no tratante) el cuerpo sale null, fail-closed
    del_admin = await client.get(f"{PREFIX}/consultations/{cid}/messages")
    assert del_admin.status_code == 200
    assert _aviso(del_admin.json())["body"] is None
    assert _aviso(del_admin.json())["kind"] == "call"


async def test_el_aviso_de_llamada_no_cuenta_como_no_leido_para_nadie(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Decisión documentada: `direction='system'` no es "la dirección contraria" de ninguno de
    los dos lados, así que el aviso NO cuenta como no leído ni para el médico ni para el
    paciente, y el marcado de leído lo ignora. Es la única lectura coherente para ambos: si
    contara para el paciente, el médico vería un no-leído por su propio clic."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    assert (
        await anon_client.post(
            f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
        )
    ).status_code == 201

    del_medico = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages", headers=auth_headers(doc.id)
    )
    assert del_medico.json()["unread_count"] == 0
    del_paciente = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages", headers={"X-Consultation-Token": token}
    )
    assert del_paciente.json()["unread_count"] == 0

    # Marcar leído no toca el aviso (no hay nada de la dirección contraria).
    marcado = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages/read", headers={"X-Consultation-Token": token}
    )
    assert marcado.status_code == 200
    assert marcado.json()["marked"] == 0

    # El hilo entra al buzón del médico, pero sin inflar el contador.
    buzon = await anon_client.get(f"{PREFIX}/inbox", headers=auth_headers(doc.id))
    assert buzon.status_code == 200
    fila = next(f for f in buzon.json() if f["consultation_id"] == cid)
    assert fila["unread_count"] == 0
    assert fila["last_direction"] == "system"


async def test_iniciar_la_llamada_no_le_escribe_al_paciente(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sobre un caso ya `in_progress` el paciente se entera por el mensaje del hilo y nada más:
    no se gasta el rate limit de correo en repetir lo que el hilo ya dice. El único correo que
    esta ruta manda es el de la cita agendada, y porque ese flujo ya lo mandaba (CA16.2b)."""
    doc = await add_doctor(db_session)
    cid, pid, _ = await _create_test_case(client, db_session, doctor=doc)
    patient = await db_session.get(Patient, uuid.UUID(pid))
    assert patient is not None
    patient.email = "paciente@example.com"
    await db_session.flush()

    enviados: list[str] = []

    async def mock_send_mail(
        to_email: str,
        subject: str,
        text: str,
        html: str = "",
        category: str = "general",
    ) -> bool:
        enviados.append(subject)
        return True

    monkeypatch.setattr(notifications, "send_mail", mock_send_mail)

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert resp.status_code == 201, resp.text
    assert enviados == []


async def test_el_cuerpo_del_aviso_de_llamada_no_lleva_ninguna_url_ni_token(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA16.6: el cuerpo es SOLO el texto del aviso — ni URL ni token, de ningún tipo.

    Este test es el que impide que la regresión vuelva. El cuerpo llevaba el enlace de
    `/entrar-videoconsulta` con un token de consulta fresco, o sea un secreto de 24 h de vida
    escrito en texto legible dentro del historial clínico y a la vista de cualquiera que mire
    la pantalla o comparta pantalla. Quien lee el hilo ya está autenticado para estar ahí: el
    acceso lo arma la interfaz con el `consultation_id` y su propia credencial.
    """
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    inicio = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert inicio.status_code == 201, inicio.text

    # Se revisan los dos lectores con grant: el cuerpo es el mismo para ambos.
    for headers in (auth_headers(doc.id), {"X-Consultation-Token": token}):
        hilo = await anon_client.get(f"{PREFIX}/consultations/{cid}/messages", headers=headers)
        assert hilo.status_code == 200
        aviso = next(m for m in hilo.json()["items"] if m["kind"] == "call")
        cuerpo = aviso["body"]
        assert cuerpo is not None

        # Nada que se parezca a un enlace...
        assert "http" not in cuerpo
        assert "://" not in cuerpo
        assert settings.JITSI_DOMAIN not in cuerpo
        assert settings.FRONTEND_URL not in cuerpo
        assert "entrar-videoconsulta" not in cuerpo
        # ...ni a un JWT (el token de consulta empieza por el `eyJ` del header base64).
        assert "eyJ" not in cuerpo
        assert "t=" not in cuerpo
        assert token not in cuerpo
        assert inicio.json()["room_url"] not in cuerpo

    # Y el texto exacto que queda en el hilo, para que cambiarlo sea una decisión consciente.
    assert cuerpo == "El médico inició la videoconsulta."
    assert cuerpo == messaging._CALL_STARTED_BODY


# =====================================================================
# 18. «Tocar el caso» no es «ser el tratante» (backlog C-13)
# =====================================================================


async def _lecturas_clinicas(db: AsyncSession, actor_id: uuid.UUID, consultation_id: str) -> int:
    """Entradas `READ_CLINICAL_DATA` de ese actor sobre el hilo de esa consulta."""
    return (
        await db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == READ_CLINICAL_DATA,
                AuditLog.actor_user_id == actor_id,
                AuditLog.resource == "messages",
                AuditLog.resource_id == consultation_id,
            )
        )
    ) or 0


async def test_el_admin_que_gestiono_el_caso_no_lee_ni_escribe_el_hilo(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    admin_identity: Profile,
) -> None:
    """C-13: gestionar el caso NO concede lectura clínica, ni aunque deje eventos suyos.

    El admin deja su fila en `consultation_events` al cambiar un estado o cerrar, y el criterio
    viejo concedía `treating_grant` por la mera existencia de esa fila: leía la conversación
    descifrada. `security.md` dice lo contrario —ser admin nunca concede lectura clínica— y
    CA3.2 exige que reciba los cuerpos en `null`. Los eventos se dejan con los endpoints
    reales, no insertándolos a mano.
    """
    doc = await add_doctor(db_session, specialty=GENERAL)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)
    secreto = "Tengo fiebre alta y manchas en la piel"
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": secreto},
    )

    # El admin (el `client` del conftest) gestiona el caso de verdad
    evento = await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": cid, "event_type": "admin_update"},
    )
    assert evento.status_code in (200, 201), evento.text
    cierre = await client.post(
        f"{PREFIX}/consultations/{cid}/close", json={"outcome": "patient_no_show"}
    )
    assert cierre.status_code == 200, cierre.text

    eventos = (await client.get(f"{PREFIX}/consultations/{cid}/events")).json()
    suyos = [e for e in eventos if e["created_by"] == str(admin_identity.id)]
    assert {e["event_type"] for e in suyos} >= {"admin_update", "patient_no_show"}, suyos

    # 1. Lee METADATOS, nunca el cuerpo (CA3.2): el hilo existe, la conversación no se le abre
    hilo = await client.get(f"{PREFIX}/consultations/{cid}/messages")
    assert hilo.status_code == 200, hilo.text
    cuerpo = hilo.json()
    assert cuerpo["clinical_access"] == "none"
    assert cuerpo["unread_count"] == 1
    assert len(cuerpo["items"]) == 1
    assert cuerpo["items"][0]["body"] is None
    assert cuerpo["items"][0]["sender_role"] == "patient"  # los metadatos sí viajan
    assert secreto not in hilo.text

    # 2. Tampoco genera una entrada de lectura concedida
    assert await _lecturas_clinicas(db_session, admin_identity.id, cid) == 0

    # 3. Ni escribe, ni adjunta, ni marca leído, ni inicia la videollamada
    # (la consulta se cerró hace un instante, así que la ventana de CA2.2 sigue abierta: el
    # 404/403 es por el grant, no por el estado)
    escribir = await client.post(
        f"{PREFIX}/consultations/{cid}/messages", json={"body": "A ver qué se dijeron"}
    )
    assert escribir.status_code == 404, escribir.text
    adjuntar = await client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        files={"file": ("informe.pdf", PDF_BYTES, "application/pdf")},
    )
    assert adjuntar.status_code == 404, adjuntar.text
    marcar = await client.post(f"{PREFIX}/consultations/{cid}/messages/read")
    assert marcar.status_code == 403, marcar.text
    llamar = await client.post(f"{PREFIX}/consultations/{cid}/video-call")
    assert llamar.status_code == 404, llamar.text

    # 4. El médico tratante no se ve afectado por el cambio
    del_medico = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages", headers=auth_headers(doc.id)
    )
    assert del_medico.json()["items"][0]["body"] == secreto
    assert del_medico.json()["clinical_access"] == "full"


async def test_medico_ajeno_con_evento_en_el_caso_tampoco_entra(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un médico que dejó un evento sin haber tomado el caso no es tratante de nada.

    `POST /consultations/{id}/events` y la derivación los puede ejercer un médico sobre un caso
    AÚN SIN ASIGNAR: con el criterio viejo, eso le abría el hilo para siempre.
    """
    tratante = await add_doctor(db_session, specialty=GENERAL)
    ajeno = await add_doctor(db_session, specialty=GENERAL)
    cid, _, token = await _create_test_case(client, db_session, doctor=tratante)
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Mensaje privado para mi médica"},
    )

    # Evento de un tipo que NO implica haber atendido (lo insertamos directo: el endpoint ya
    # exige pertenencia, y lo que se prueba aquí es el criterio del grant, no esa puerta)
    db_session.add(
        ConsultationEvent(
            id=uuid.uuid4(),
            consultation_id=uuid.UUID(cid),
            event_type="derived",
            created_by=ajeno.id,
        )
    )
    await db_session.flush()

    lectura = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages", headers=auth_headers(ajeno.id)
    )
    assert lectura.status_code == 404, lectura.text
    escribir = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(ajeno.id),
        json={"body": "Déjame ver"},
    )
    assert escribir.status_code == 404, escribir.text
    llamar = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(ajeno.id)
    )
    assert llamar.status_code == 404, llamar.text


async def test_la_lectura_concedida_queda_auditada_una_vez_por_pagina(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    admin_identity: Profile,
) -> None:
    """CA3.3: una entrada `READ_CLINICAL_DATA` por página leída, no una por mensaje.

    Es la traza de no repudio del hilo: si un refactor se llevara la llamada a
    `audit_clinical_read`, nadie lo notaría sin este test.
    """
    doc = await add_doctor(db_session, specialty=GENERAL)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)
    for i in range(3):
        await anon_client.post(
            f"{PREFIX}/consultations/{cid}/messages",
            headers={"X-Consultation-Token": token},
            json={"body": f"Mensaje {i}"},
        )

    assert await _lecturas_clinicas(db_session, doc.id, cid) == 0

    lectura = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages", headers=auth_headers(doc.id)
    )
    assert lectura.status_code == 200
    assert len(lectura.json()["items"]) == 3
    assert await _lecturas_clinicas(db_session, doc.id, cid) == 1  # una por página, no tres

    # Una segunda página leída suma otra entrada (la traza cuenta accesos, no mensajes)
    await anon_client.get(f"{PREFIX}/consultations/{cid}/messages", headers=auth_headers(doc.id))
    assert await _lecturas_clinicas(db_session, doc.id, cid) == 2

    # El admin, sin grant, no deja entrada de lectura concedida
    await client.get(f"{PREFIX}/consultations/{cid}/messages")
    assert await _lecturas_clinicas(db_session, admin_identity.id, cid) == 0


async def test_la_llamada_inicia_la_cita_agendada_y_manda_su_correo(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CA16.2b: una cita agendada se abre desde aquí, con el correo que ese flujo ya mandaba.

    Al retirarse el botón «Unirse a videoconsulta» del detalle, este endpoint es el único camino
    a la sala: si no hiciera la transición, una cita de la Agenda se quedaría sin forma de
    empezar (`scheduled` no está entre los estados de `ensure_video_room`, así que daría 409).

    Se reutiliza `start_scheduled_consultation` tal cual —su UPDATE condicional y su evento
    `opened`— y el correo se encola en el router con `BackgroundTasks`, igual que en
    `POST /consultations/{id}/start`.
    """
    doc = await add_doctor(db_session)
    cid, pid, _ = await _create_test_case(client, db_session, doctor=doc, status="scheduled")
    patient = await db_session.get(Patient, uuid.UUID(pid))
    assert patient is not None
    patient.email = "agenda@example.com"
    await db_session.flush()

    avisos: list[dict] = []

    async def _fake_video_ready(**kwargs) -> bool:
        avisos.append(kwargs)
        return True

    monkeypatch.setattr(notifications, "send_video_ready_email", _fake_video_ready)

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert resp.status_code == 201, resp.text

    consulta = await db_session.get(Consultation, uuid.UUID(cid))
    assert consulta is not None
    await db_session.refresh(consulta)
    assert consulta.status == "in_progress"
    assert consulta.opened_at is not None
    assert consulta.video_room_url == resp.json()["room_url"]

    # El evento del claim de la Agenda, que es lo que hace de este médico el tratante.
    eventos = (
        (
            await db_session.execute(
                select(ConsultationEvent).where(
                    ConsultationEvent.consultation_id == consulta.id,
                    ConsultationEvent.event_type == "opened",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(eventos) == 1

    # El correo "tu médico ya está en la sala", con su enlace tokenizado: ahí SÍ hace falta,
    # porque el destinatario no está autenticado (CA16.6 solo prohíbe el enlace en el cuerpo
    # del mensaje del hilo).
    assert len(avisos) == 1
    assert avisos[0]["to_email"] == "agenda@example.com"
    assert "/entrar-videoconsulta?c=" in avisos[0]["join_url"]
    assert settings.JITSI_DOMAIN not in avisos[0]["join_url"]

    # Y el aviso en el hilo, uno solo.
    assert len(await _system_call_messages(db_session, cid)) == 1


async def test_sobre_la_cita_ya_abierta_no_se_repite_ni_el_correo_ni_el_evento(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reentrar a una cita que esta misma ruta ya abrió no vuelve a abrirla.

    La transición solo ocurre si la consulta sigue en `scheduled`, así que el segundo clic no
    manda otro correo ni escribe otro evento `opened` — y tampoco falla con 409, que sería
    absurdo para quien solo quiere volver a entrar a su sala.
    """
    doc = await add_doctor(db_session)
    cid, pid, _ = await _create_test_case(client, db_session, doctor=doc, status="scheduled")
    patient = await db_session.get(Patient, uuid.UUID(pid))
    assert patient is not None
    patient.email = "agenda@example.com"
    await db_session.flush()

    avisos: list[dict] = []

    async def _fake_video_ready(**kwargs) -> bool:
        avisos.append(kwargs)
        return True

    monkeypatch.setattr(notifications, "send_video_ready_email", _fake_video_ready)

    primera = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    segunda = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert primera.status_code == 201, primera.text
    assert segunda.status_code == 201, segunda.text
    assert primera.json()["room_url"] == segunda.json()["room_url"]
    assert primera.json()["message_id"] == segunda.json()["message_id"]

    assert len(avisos) == 1, "el correo de la cita agendada sale una sola vez"
    eventos = await db_session.scalar(
        select(func.count(ConsultationEvent.id)).where(
            ConsultationEvent.consultation_id == uuid.UUID(cid),
            ConsultationEvent.event_type == "opened",
        )
    )
    assert eventos == 1
    assert len(await _system_call_messages(db_session, cid)) == 1
    # Los dos intentos sí quedan en el audit.
    assert len(await _call_started_entries(db_session, cid)) == 2


async def test_la_presencia_del_paciente_no_condiciona_la_llamada(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA16.2 (revisado el 2026-10-06): el backend nunca gateó por presencia y sigue sin hacerlo.

    El candado era del frontend y se retiró al quedar un solo botón: con el antiguo «Unirse a
    videoconsulta» fuera, exigir al paciente conectado dejaría sin camino a la sala justo los
    casos que ese botón cubría (la cita agendada y la reentrada). La presencia sigue siendo
    información para que el médico decida, no condición.
    """
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)

    # Nadie registró actividad del paciente: no está en línea por ninguna de las dos señales.
    consulta = await db_session.get(Consultation, uuid.UUID(cid))
    assert consulta is not None
    assert consulta.patient_last_seen_at is None
    assert messaging.is_patient_online(consulta.id, None) is False

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/video-call", headers=auth_headers(doc.id)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["room_url"]
    assert len(await _system_call_messages(db_session, cid)) == 1
