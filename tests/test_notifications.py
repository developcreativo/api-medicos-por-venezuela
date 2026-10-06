"""Composición de los cuerpos de correo de `services/notifications.py`.

Son funciones puras (reciben valores planos, devuelven `(asunto, texto, html)`), así que se
prueban sin sesión y sin IO. Lo que se fija aquí es la frontera de SEGURIDAD: nada de lo que
teclea una persona puede salir como marcado vivo en el HTML.
"""

import inspect
import uuid
from datetime import UTC, datetime

from src.core import consultation_token
from src.core.config import settings
from src.services import notifications

# Un enlace completo, no un `<script>`: los clientes de correo no ejecutan JS, pero sí pintan
# un `<a>`. El ataque realista es un enlace con pinta de botón de la plataforma.
VENENO = '<a href="http://malicioso.example">Aprobar ahora</a>'
CUANDO = datetime(2026, 9, 10, 15, 30, tzinfo=UTC)


def _sin_enlace_vivo(html: str) -> None:
    """El dato se conserva, pero inerte: escapado y sin abrir un `<a>` propio."""
    assert "&lt;a href=" in html  # aserción POSITIVA: está escapado, no solo ausente
    assert "malicioso.example" in html  # ...y el dato no se perdió por el camino
    assert '<a href="http://malicioso.example">' not in html


def test_correo_de_cita_escapa_al_paciente_y_al_medico() -> None:
    """SEGURIDAD. `patient_name` sale del formulario PÚBLICO y sin autenticar de la cola
    (`POST /patients`) y `doctor_name` del perfil que el propio médico edita. Sin escapar,
    cualquiera se registra con `<a href="http://malo/">...</a>` de nombre y le mete un enlace
    vivo, con apariencia de venir de la plataforma, al paciente que recibe la cita: phishing
    servido por nosotros.
    """
    _, text, html = notifications._build_email(
        patient_name=VENENO, code="CONS-2026-1", when=CUANDO, doctor_name=None, is_reminder=False
    )
    _sin_enlace_vivo(html)
    assert VENENO in text  # el texto plano no se toca: ahí un `<a>` no es marcado

    _, _, html_medico = notifications._build_email(
        patient_name="María Pérez",
        code="CONS-2026-1",
        when=CUANDO,
        doctor_name=VENENO,
        is_reminder=True,
    )
    _sin_enlace_vivo(html_medico)


def test_difusion_de_interconsulta_escapa_la_edad() -> None:
    """SEGURIDAD. El rango etario lo teclea un médico en el alta de su paciente y esta difusión
    sale a TODOS los especialistas de una especialidad: un solo caso mal intencionado alcanza
    cientos de bandejas."""
    _, text, html = notifications.interconsultation_broadcast_email(
        specialty_name="Cardiología", age_range=VENENO
    )
    _sin_enlace_vivo(html)
    assert VENENO in text
    # El enlace legítimo al panel sigue siendo un enlace: escapar no puede romper el correo.
    assert f'<a href="{notifications.panel_url()}">' in html


def test_correos_de_interconsulta_no_aceptan_texto_clinico() -> None:
    """Decisión 2026-09-23: nada de texto clínico fuera de la API. Un correo queda en buzones,
    reenvíos y backups ajenos; el motivo se lee en el panel, con permiso y audit. Que la firma
    ni siquiera acepte el motivo impide que alguien lo vuelva a pasar "solo un extracto"."""
    for fn in (
        notifications.interconsultation_broadcast_email,
        notifications.interconsultation_taken_email,
    ):
        assert "chief_complaint" not in inspect.signature(fn).parameters

    subject, text, html = notifications.interconsultation_broadcast_email("Cardiología", "30-39")
    assert "Motivo" not in text + html + subject
    assert "Cardiología" in text and notifications.panel_url() in text

    subject, text, html = notifications.interconsultation_taken_email("Dra. Rivas", "Cardiología")
    assert "Motivo" not in text + html + subject
    assert "Cardiología" in text and notifications.panel_url() in text


def test_aviso_de_caso_tomado_escapa_al_especialista() -> None:
    """SEGURIDAD. El nombre del especialista viene de su propio perfil y acaba en el HTML del
    aviso."""
    _, _, html = notifications.interconsultation_taken_email(
        specialist_name=VENENO, specialty_name="Cardiología"
    )
    _sin_enlace_vivo(html)


def test_sin_nombre_de_especialista_cae_a_la_especialidad() -> None:
    """El fallback también pasa por el HTML: si el perfil no tiene nombre, el correo dice la
    especialidad en su lugar y no un `None`."""
    subject, text, html = notifications.interconsultation_taken_email(
        specialist_name=None, specialty_name="Cardiología"
    )
    assert "Un especialista en Cardiología" in text
    assert "<strong>Un especialista en Cardiología</strong>" in html
    assert "None" not in html
    assert subject


# --- "Tu médico ya está en la sala" (el correo que dispara el claim por video) ---

# El enlace del correo NO va a Jitsi: va a `/entrar-videoconsulta`, que registra la entrada y
# redirige. Lleva dos parámetros, es decir un `&`, que dentro de un `href` tiene que salir como
# `&amp;` — el detalle exacto que rompe un enlace en la mitad de los clientes sin que nada chille.
ENTRAR = "https://medicosporvenezuela.org/entrar-videoconsulta?c=abc&t=jwt.de.prueba"


def test_el_aviso_de_videoconsulta_lleva_el_enlace_como_boton_y_en_claro() -> None:
    """El correo existe para que el paciente ENTRE a la sala: el enlace es su única razón de
    ser. Va dos veces a propósito — como botón y en claro— porque hay clientes que no pintan
    el botón, y quedarse sin forma de llegar sería el mismo problema que esto viene a resolver.
    """
    subject, text, html = notifications.video_ready_email(
        "María Pérez", "Dr. Rivas", ENTRAR, "CONS-2026-1"
    )
    assert "esperando" in subject.lower()
    assert ENTRAR in text
    assert html.count(f'href="{notifications.esc(ENTRAR)}"') == 2
    assert "Entrar a la videoconsulta" in html
    assert "CONS-2026-1" in text and "CONS-2026-1" in html


def test_el_aviso_de_videoconsulta_escapa_los_nombres() -> None:
    """SEGURIDAD. `patient_name` sale del formulario PÚBLICO de la cola y `doctor_name` del
    perfil que el propio médico edita: los mismos dos vectores del correo de cita."""
    _, text, html = notifications.video_ready_email(VENENO, "Dr. Rivas", ENTRAR, "CONS-2026-1")
    _sin_enlace_vivo(html)
    assert VENENO in text

    _, _, html_medico = notifications.video_ready_email("María", VENENO, ENTRAR, "CONS-2026-1")
    _sin_enlace_vivo(html_medico)


def test_el_aviso_de_videoconsulta_sin_nombres_no_dice_none() -> None:
    """El nombre del paciente y el del médico son opcionales en la base. Un correo que
    saludara "Hola None" es peor que uno impersonal."""
    _, text, html = notifications.video_ready_email(None, None, ENTRAR, None)
    assert "None" not in html
    assert "None" not in text
    assert "Tu médico" in text


def test_el_enlace_de_entrada_va_escapado_dentro_del_href() -> None:
    """El `&` que separa `c` de `t` tiene que salir como `&amp;`. Sin eso el enlace queda mal
    formado y hay clientes que lo cortan justo ahí: el paciente aterrizaría en la página de
    entrada sin token, es decir en un 401, con su médico esperando dentro de la sala."""
    _, _, html = notifications.video_ready_email("María", "Dr. Rivas", ENTRAR, "CONS-2026-1")
    assert "&amp;t=jwt.de.prueba" in html
    assert "abc&t=" not in html  # el crudo no puede haberse colado


def test_el_enlace_de_entrada_pasa_por_el_sitio_y_no_por_jitsi() -> None:
    """El salto por `/entrar-videoconsulta` es lo único que hace que la plataforma se entere de
    que el paciente entró. Con un enlace directo a Jitsi el paciente entra igual, pero el médico
    —que está dentro esperando— sigue sin saber si viene: es el reporte que originó esto.

    El token se emite FRESCO aquí y no se reutiliza el del registro: ese caduca a las 24 h y
    este correo puede salir mucho después."""
    consultation_id = uuid.uuid4()
    url = notifications.build_join_url(consultation_id)

    assert url.startswith(f"{settings.FRONTEND_URL}/entrar-videoconsulta?")
    assert settings.JITSI_DOMAIN not in url
    assert f"c={consultation_id}" in url
    token = url.split("t=")[1]
    assert consultation_token.is_valid_for(token, consultation_id)
    # Y solo para ESA consulta: un token válido de la propia sala no puede abrir la de otro.
    assert not consultation_token.is_valid_for(token, uuid.uuid4())


def test_catalogo_incluye_message_received() -> None:
    """R6: El catálogo de preferencias de notificación incluye `message_received` en email."""
    assert "message_received" in notifications.NOTIFICATION_EVENTS
    assert notifications.NOTIFICATION_EVENTS["message_received"] == ("email",)


def test_correo_mensajeria_asercion_negativa_sin_cuerpo_clinico() -> None:
    """R6 (P7): TEST NEGATIVO — Cero texto clínico ni datos sensibles en el correo.

    El correo es un canal no cifrado en tránsito ni en reposo. Jamás debe llevar:
    - Cuerpo del mensaje escrito por paciente o médico.
    - Diagnósticos, síntomas o motivos de consulta.
    - Nombres de archivos adjuntos.
    Solo lleva el aviso genérico y el enlace seguro para responder en la plataforma.
    """
    cid = uuid.uuid4()
    cuerpo_clinico_secreto = "Tengo fiebre de 39 y dolor lumbar agudo severo"
    adjunto_secreto = "radiografia_torax_paciente.pdf"

    # 1. Correo al médico
    subj_doc, text_doc, html_doc = notifications.doctor_message_received_email(
        consultation_id=cid, code="CONS-MED-001"
    )
    assert subj_doc == "Tu paciente te escribió"
    assert f"/panel-medico/consulta/{cid}" in text_doc
    assert f"/panel-medico/consulta/{cid}" in html_doc
    assert "CONS-MED-001" in text_doc and "CONS-MED-001" in html_doc

    # Aserción negativa estricta
    for doc_content in (subj_doc, text_doc, html_doc):
        assert cuerpo_clinico_secreto not in doc_content
        assert adjunto_secreto not in doc_content
        assert "fiebre" not in doc_content.lower()

    # 2. Correo al paciente (anónimo con token)
    subj_pat, text_pat, html_pat = notifications.patient_message_received_email(
        consultation_id=cid, code="CONS-MED-001", has_account=False
    )
    assert subj_pat == "Tu médico te respondió"
    assert f"/sala-espera?cid={cid}&t=" in text_pat
    assert f"/sala-espera?cid={cid}&amp;t=" in html_pat

    for pat_content in (subj_pat, text_pat, html_pat):
        assert cuerpo_clinico_secreto not in pat_content
        assert adjunto_secreto not in pat_content
        assert "fiebre" not in pat_content.lower()

    # 3. Correo al paciente con cuenta
    _, text_pat_acc, html_pat_acc = notifications.patient_message_received_email(
        consultation_id=cid, code="CONS-MED-001", has_account=True
    )
    assert "/mi-caso" in text_pat_acc
    assert "/mi-caso" in html_pat_acc


def test_correo_mensajeria_escapa_codigo_venenoso() -> None:
    """SEGURIDAD: Si el código de consulta o el enlace contienen marcado vivo, va escapado."""
    cid = uuid.uuid4()
    _, text, html = notifications.doctor_message_received_email(consultation_id=cid, code=VENENO)
    _sin_enlace_vivo(html)
    assert VENENO in text
