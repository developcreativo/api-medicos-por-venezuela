"""Notificaciones de citas agendadas (email), sobre services/mail.py (best-effort).

Se usa en dos momentos del módulo Agenda:
- Al agendar (seguimiento o referencia): el router encola el correo con BackgroundTasks.
- ~30 min antes: `send_due_reminders` (endpoint gateado + cron externo, idempotente por
  `reminder_sent_at`).

Un fallo de correo NUNCA rompe el agendado (ver mail.send_mail). Solo se le escribe al paciente si
tiene email (`patients.email` es opcional).
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import consultation_token
from src.core.config import settings
from src.core.errors import NotFoundError
from src.core.tz import VET
from src.models.clinical import Message
from src.models.consultation import Consultation
from src.models.patient import Patient
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.services import mail_layout
from src.services.mail import best_effort, esc, send_mail

logger = logging.getLogger("mpv.api")

# --- Preferencias de notificación (para que el sistema no sea invasivo) ---
# Catálogo de notificaciones configurables por el médico (Ajustes → preferencias). Cada evento
# declara los canales que le aplican. Es la FUENTE DE VERDAD de las keys; el frontend replica las
# etiquetas en español (lib/notificationPrefs.ts). Opt-out: preferencia ausente = habilitada.
NOTIFICATION_EVENTS: dict[str, tuple[str, ...]] = {
    "appointment_reminder": ("push", "email"),  # recordatorio ~30 min antes de una cita
    "appointment_confirm": ("push",),  # aviso al agendar/referir (tu propia acción)
    # Interconsulta/referencia: canal EMAIL (el destinatario no está en la página cuando ocurre; el
    # push nativo requeriría realtime + pestaña abierta — pendiente). Se declara lo que sí existe.
    "interconsultation_assigned": ("email",),  # un colega te invita a una interconsulta
    "referral_received": ("email",),  # te refieren/agendan como especialista
    # Interconsulta ASÍNCRONA (pacientes de consultorio). Canal email por lo mismo: el
    # destinatario no está en la página cuando ocurre.
    "interconsultation_request_broadcast": ("email",),  # buscan tu especialidad para un caso
    "interconsultation_request_taken": ("email",),  # un especialista tomó tu caso
    "message_received": ("email",),  # aviso por email cuando llega un mensaje nuevo en un hilo
}


def should_send(prefs: dict | None, event: str, channel: str) -> bool:
    """¿El usuario quiere `event` por `channel`? Opt-out: ausente = habilitado. False si el canal
    no aplica al evento según el catálogo (evita mandar por un canal no contemplado)."""
    if channel not in NOTIFICATION_EVENTS.get(event, ()):
        return False
    if not prefs:
        return True
    return bool(prefs.get(event, {}).get(channel, True))


def sanitize_prefs(prefs: dict) -> dict:
    """Limpia la entrada (no confiable): deja solo eventos/canales del catálogo, valores bool."""
    clean: dict = {}
    for event, channels in NOTIFICATION_EVENTS.items():
        raw = prefs.get(event)
        if isinstance(raw, dict):
            sub = {ch: bool(raw[ch]) for ch in channels if ch in raw}
            if sub:
                clean[event] = sub
    return clean


async def get_prefs(session: AsyncSession, user_id) -> dict:
    user = await session.get(Profile, user_id)
    if user is None:
        raise NotFoundError("Usuario no encontrado.")
    return user.notification_prefs or {}


async def set_prefs(session: AsyncSession, user_id, prefs: dict) -> dict:
    user = await session.get(Profile, user_id)
    if user is None:
        raise NotFoundError("Usuario no encontrado.")
    user.notification_prefs = sanitize_prefs(prefs)
    await session.commit()
    return user.notification_prefs


def fmt_when(when: datetime) -> str:
    """Fecha/hora de la cita en hora de Venezuela, legible para el paciente.

    El huso vive en `src/core/tz.py` (una sola definición para los correos y para los reportes
    en Excel). Ahí está también el porqué de un offset fijo en vez de `zoneinfo`."""
    return when.astimezone(VET).strftime("%d/%m/%Y %I:%M %p")


def _build_email(
    patient_name: str, code: str, when: datetime, doctor_name: str | None, is_reminder: bool
) -> tuple[str, str, str]:
    """(subject, text, html) del correo de cita. Sin motor de plantillas: strings simples.

    Todo lo que se interpola en el HTML pasa por `mail.esc`: `patient_name` sale del formulario
    PÚBLICO de la cola y `doctor_name` del perfil que el propio médico edita. Ver el porqué
    completo en el docstring de `esc`. El texto plano no lo necesita.
    """
    cita = fmt_when(when)
    con_quien = f" con {doctor_name}" if doctor_name else ""
    con_quien_html = f" con {esc(doctor_name)}" if doctor_name else ""
    if is_reminder:
        subject = f"Recordatorio: tu cita médica es pronto ({cita})"
        intro = "Te recordamos que tu cita médica es pronto."
    else:
        subject = f"Cita agendada para el {cita}"
        intro = "Tu cita médica ha sido agendada."
    text = (
        f"Hola {patient_name},\n\n{intro}\n\n"
        f"Fecha y hora: {cita}{con_quien}\n"
        f"Código de caso: {code}\n\n"
        "Ingresa a tu cuenta en Médicos por Venezuela para ver los detalles.\n"
    )
    html = (
        f"<p>Hola {esc(patient_name)},</p><p>{intro}</p>"
        f"<p><strong>Fecha y hora:</strong> {cita}{con_quien_html}<br>"
        f"<strong>Código de caso:</strong> {esc(code)}</p>"
        "<p>Ingresa a tu cuenta en Médicos por Venezuela para ver los detalles.</p>"
    )
    return subject, text, html


async def send_appointment_email(
    to_email: str,
    patient_name: str | None,
    code: str,
    when: datetime,
    doctor_name: str | None = None,
    is_reminder: bool = False,
) -> bool:
    """Envía el correo de una cita (agendada o recordatorio). Best-effort (ver send_mail)."""
    subject, text, html = _build_email(
        patient_name or "paciente", code, when, doctor_name, is_reminder
    )
    category = "recordatorio" if is_reminder else "cita"
    return await send_mail(to_email, subject, text, html, category=category)


async def _doctor_name(session: AsyncSession, doctor_id) -> str | None:
    if doctor_id is None:
        return None
    return await session.scalar(select(Profile.full_name).where(Profile.id == doctor_id))


# --- "Tu médico ya está en la sala" (el médico tomó el caso por videoconsulta) ---
#
# El paciente ve el botón de entrar en `/sala-espera`, la pantalla a la que cae justo después de
# registrarse. Pero ese enlace vive SOLO en esa pestaña: `/mi-caso` no muestra la sala, y el
# correo de registro tampoco existía. Quien cerró la pestaña —o entró, no había nadie todavía y
# se salió— se quedaba sin forma de volver, y el médico entraba a una sala vacía.
#
# Este correo es lo que cierra ese hueco: sale cuando un médico toma el caso POR VIDEO, que es
# el único momento en que se sabe que hay alguien esperando del otro lado.

# Lo mínimo para que la videollamada no se caiga por el camino. Es el mismo contenido del modal
# de `/sala-espera`, resumido: al llegar por correo el paciente se salta esa pantalla.
_CONSEJOS_SALA = (
    "Elige «Continuar en el navegador»: no necesitas descargar ninguna aplicación.",
    "Pulsa «Permitir» cuando te pida la cámara y el micrófono.",
    "Escribe tu nombre completo si la videollamada te lo pide.",
)


def video_ready_email(
    patient_name: str | None, doctor_name: str | None, join_url: str, code: str | None
) -> tuple[str, str, str]:
    """(subject, text, html) del aviso de que el médico ya está en la sala.

    El botón NO apunta a Jitsi directamente, sino a `/entrar-videoconsulta` (ver `join_url`),
    que registra la entrada y redirige sola. Un enlace directo ahorraría ese salto, pero
    entonces el paciente entraría a la sala sin que la plataforma se enterara — y el médico,
    que está esperando dentro, seguiría viendo "sin conexión" sin saber si viene o no. El salto
    es invisible: la página redirige sin pedir nada.

    Ese registro pasa por JavaScript a propósito, y no por un redirect del backend: los
    escáneres de correo corporativos SIGUEN los enlaces de un mensaje para analizarlos, así que
    un `GET` que marcara la entrada daría "el paciente entró" por un robot. Un escáner no
    ejecuta JavaScript; una persona sí.

    El mismo enlace va como botón y en claro: hay clientes que no pintan el botón, y quedarse
    sin forma de llegar sería el problema que este correo viene a resolver.
    """
    quien = doctor_name or "Tu médico"
    nombre = patient_name or "paciente"
    subject = "Tu médico te está esperando en la videoconsulta"
    consejos_text = "\n".join(f"  - {c}" for c in _CONSEJOS_SALA)
    codigo_text = f"Código de caso: {code}\n" if code else ""
    text = (
        f"Hola {nombre},\n\n"
        f"{quien} ya entró a la sala de tu videoconsulta y te está esperando.\n\n"
        f"Entra ahora desde este enlace:\n{join_url}\n\n"
        f"Para que funcione bien:\n{consejos_text}\n\n"
        f"{codigo_text}"
        "Si tu situación empeora o hay señales de alarma, busca atención presencial urgente.\n"
    )
    url = esc(join_url)
    consejos_html = "".join(f"<li>{esc(c)}</li>" for c in _CONSEJOS_SALA)
    codigo_html = (
        f'<p style="color:{mail_layout.MUTED};font-size:14px;">'
        f"<strong>Código de caso:</strong> {esc(code)}</p>"
        if code
        else ""
    )
    html = (
        f"<p>Hola {esc(nombre)},</p>"
        f"<p><strong>{esc(quien)}</strong> ya entró a la sala de tu videoconsulta y te está "
        "esperando.</p>"
        f'<p style="margin:26px 0;">{mail_layout.button(url, "Entrar a la videoconsulta")}</p>'
        "<p><strong>Para que funcione bien:</strong></p>"
        f'<ul style="padding-left:20px;margin:0 0 18px;">{consejos_html}</ul>'
        f"{codigo_html}"
        # El enlace en claro, además del botón: hay clientes que no pintan el botón (o el
        # paciente lo abre desde un móvil donde el correo se ve en texto), y quedarse sin
        # ninguna forma de llegar a la sala sería el mismo problema que este correo resuelve.
        f'<p style="color:{mail_layout.MUTED};font-size:13px;word-break:break-all;">'
        f'Si el botón no funciona, copia este enlace: <a href="{url}" '
        f'style="color:{mail_layout.BLUE};">{url}</a></p>'
        '<p style="background:#fff7ed;border-left:4px solid #f59e0b;padding:12px 14px;'
        'margin:22px 0 0;font-size:14px;">Si tu situación empeora o hay señales de alarma, '
        "busca atención presencial urgente.</p>"
    )
    return subject, text, html


def build_join_url(consultation_id) -> str:
    """Enlace de entrada a la videoconsulta para el correo.

    Apunta a `/entrar-videoconsulta`, una página del sitio que registra la entrada
    (`POST /consultations/{id}/entered-call`), pide la sala y redirige sola. No lleva la URL de
    Jitsi en la query a propósito: un parámetro con el destino convierte esta página en un
    redirector abierto, y además el enlace del correo se vuelve ilegible.

    El token se emite **aquí**, fresco. El que se le dio al paciente al registrarse caduca a las
    24 h y este correo puede salir mucho después: reutilizarlo mandaría a la mitad de la gente a
    un 401 justo cuando su médico la está esperando.
    """
    base = settings.FRONTEND_URL.rstrip("/")
    token = consultation_token.issue(consultation_id)
    return f"{base}/entrar-videoconsulta?c={consultation_id}&t={token}"


async def video_ready_mail_args(session: AsyncSession, consultation: Consultation) -> dict | None:
    """Args para `send_video_ready_email`, o None si no hay a quién o a dónde escribirle.

    Dos motivos para no mandarlo, y ninguno es un fallo: el paciente no tiene correo (es
    opcional en `patients`, y los de consultorio suelen no tenerlo), o la consulta no llegó a
    tener sala. Lo segundo no debería pasar —el panel crea la sala ANTES del claim— pero si
    pasa, un correo que anuncia una videollamada sin enlace es peor que ninguno.

    Se resuelve con la sesión viva y devuelve valores planos: el BackgroundTask corre después
    de cerrar la request, cuando ya no se puede consultar la base (mismo patrón que
    `appointment_email_args`).
    """
    if not consultation.video_room_url:
        logger.warning("MAIL:skip reason=consulta_sin_sala consultation_id=%s", consultation.id)
        return None
    patient = await session.get(Patient, consultation.patient_id)
    if patient is None or not patient.email:
        return None
    return {
        "to_email": patient.email,
        "patient_name": patient.full_name,
        "doctor_name": await _doctor_name(session, consultation.assigned_doctor_id),
        "join_url": build_join_url(consultation.id),
        "code": consultation.code,
    }


@best_effort
async def send_video_ready_email(
    to_email: str,
    patient_name: str | None,
    doctor_name: str | None,
    join_url: str,
    code: str | None,
) -> bool:
    """Envía el aviso de "tu médico ya está en la sala". Best-effort (ver send_mail)."""
    subject, text, html = video_ready_email(patient_name, doctor_name, join_url, code)
    return await send_mail(to_email, subject, text, html, category="videoconsulta")


# --- "Tu caso pasó a la cola de X" (derivación a otra especialidad) ---
#
# Sin esto el paciente derivado no se enteraba: seguía mirando una sala de espera que ya no era
# la suya, y algunos terminaban creando otra cuenta y otra consulta.


def build_waiting_room_url(consultation_id) -> str:
    """Enlace a `/sala-espera` del caso, con un token fresco (el del registro caduca a las 24 h).
    La página muestra el estado en vivo y el botón de entrar cuando un médico lo toma."""
    base = settings.FRONTEND_URL.rstrip("/")
    token = consultation_token.issue(consultation_id)
    return f"{base}/sala-espera?cid={consultation_id}&t={token}"


def derivation_email(
    patient_name: str | None, specialty_name: str, waiting_url: str, code: str | None
) -> tuple[str, str, str]:
    """(subject, text, html) del aviso al paciente de que su caso pasó a otra especialidad."""
    nombre = patient_name or "paciente"
    subject = f"Tu caso pasó a la cola de {specialty_name}"
    codigo_text = f"Código de caso: {code}\n" if code else ""
    text = (
        f"Hola {nombre},\n\n"
        f"Tu caso pasó a la cola de {specialty_name}. Conservas tu lugar en la fila.\n\n"
        "Cuando un especialista tome tu caso te llegará otro correo para entrar a la "
        "videoconsulta. Hay muchos pacientes esperando, así que puede tardar: no hace falta "
        "que vuelvas a registrarte.\n\n"
        f"Puedes ver el estado de tu caso aquí:\n{waiting_url}\n\n"
        f"{codigo_text}"
        "Si tu situación empeora o hay señales de alarma, busca atención presencial urgente.\n"
    )
    url = esc(waiting_url)
    codigo_html = (
        f'<p style="color:{mail_layout.MUTED};font-size:14px;">'
        f"<strong>Código de caso:</strong> {esc(code)}</p>"
        if code
        else ""
    )
    html = (
        f"<p>Hola {esc(nombre)},</p>"
        f"<p>Tu caso pasó a la cola de <strong>{esc(specialty_name)}</strong>. Conservas tu "
        "lugar en la fila.</p>"
        "<p>Cuando un especialista tome tu caso te llegará otro correo para entrar a la "
        "videoconsulta. Hay muchos pacientes esperando, así que puede tardar: no hace falta que "
        "vuelvas a registrarte.</p>"
        f'<p style="margin:26px 0;">{mail_layout.button(url, "Ver el estado de mi caso")}</p>'
        f"{codigo_html}"
        f'<p style="color:{mail_layout.MUTED};font-size:13px;word-break:break-all;">'
        f'Si el botón no funciona, copia este enlace: <a href="{url}" '
        f'style="color:{mail_layout.BLUE};">{url}</a></p>'
        '<p style="background:#fff7ed;border-left:4px solid #f59e0b;padding:12px 14px;'
        'margin:22px 0 0;font-size:14px;">Si tu situación empeora o hay señales de alarma, '
        "busca atención presencial urgente.</p>"
    )
    return subject, text, html


async def derivation_mail_args(session: AsyncSession, consultation: Consultation) -> dict | None:
    """Args para `send_derivation_email`, o None si el paciente no tiene correo. Se resuelve con
    la sesión viva (el BackgroundTask corre tras cerrar la request)."""
    patient = await session.get(Patient, consultation.patient_id)
    if patient is None or not patient.email:
        return None
    specialty_name = await session.scalar(
        select(Specialty.name).where(Specialty.id == consultation.specialty_id)
    )
    return {
        "to_email": patient.email,
        "patient_name": patient.full_name,
        "specialty_name": specialty_name or "otra especialidad",
        "waiting_url": build_waiting_room_url(consultation.id),
        "code": consultation.code,
    }


@best_effort
async def send_derivation_email(
    to_email: str,
    patient_name: str | None,
    specialty_name: str,
    waiting_url: str,
    code: str | None,
) -> bool:
    """Envía el aviso de derivación. Best-effort (ver send_mail)."""
    subject, text, html = derivation_email(patient_name, specialty_name, waiting_url, code)
    return await send_mail(to_email, subject, text, html, category="derivacion")


async def appointment_email_args(session: AsyncSession, consultation: Consultation) -> dict | None:
    """Args para `send_appointment_email` de una cita recién agendada, o None si el paciente no
    tiene email. Se resuelve DENTRO del request (sesión viva) para pasar valores planos al
    BackgroundTask (que corre tras cerrar la request)."""
    patient = await session.get(Patient, consultation.patient_id)
    if patient is None or not patient.email:
        return None
    return {
        "to_email": patient.email,
        "patient_name": patient.full_name,
        "code": consultation.code,
        "when": consultation.scheduled_at,
        "doctor_name": await _doctor_name(session, consultation.assigned_doctor_id),
    }


async def doctor_event_email_args(
    session: AsyncSession, *, user_id, event: str, subject: str, text: str, html: str | None = None
) -> dict | None:
    """Args para `send_mail` de `event` a un médico, o None si no tiene email o lo desactivó
    (opt-out). Espejo de `appointment_email_args`: el router encola con BackgroundTasks."""
    user = await session.get(Profile, user_id)
    if user is None or not user.email:
        return None
    if not should_send(user.notification_prefs, event, "email"):
        return None
    return {
        "to_email": user.email,
        "subject": subject,
        "text": text,
        "html": html,
        "category": event.replace("_", "-"),
    }


def _build_doctor_reminder(patient_name: str | None, code: str, when: datetime) -> tuple[str, str]:
    """(subject, text) del recordatorio de cita para el MÉDICO (no el correo del paciente)."""
    cita = fmt_when(when)
    subject = f"Recordatorio: cita con {patient_name or 'un paciente'} ({cita})"
    text = (
        f"Tienes una cita próxima.\n\nPaciente: {patient_name or 'N/D'}\n"
        f"Fecha y hora: {cita}\nCódigo de caso: {code}\n\n"
        "Ingresa a tu panel en Médicos por Venezuela.\n"
    )
    return subject, text


async def send_due_reminders(session: AsyncSession, window_minutes: int = 30) -> int:
    """Manda el recordatorio de las citas 'scheduled' cuya hora cae en [ahora, ahora+ventana] y aún
    no tienen `reminder_sent_at`. Devuelve cuántos correos aceptó Mailtrap. Para invocarse desde un
    CRON externo. Idempotente: marca `reminder_sent_at` tras el intento, así una 2ª corrida no
    reenvía. ponytail: 1 solo intento — si el correo falla (best-effort) NO se reintenta (evita
    spam); el correo confiable es el 'al agendar', esto es el recordatorio complementario."""
    now = datetime.now(UTC)
    horizon = now + timedelta(minutes=window_minutes)
    due_stmt = (
        select(
            Consultation,
            Patient.email.label("email"),
            Patient.full_name.label("patient_name"),
            Profile.full_name.label("doctor_name"),
            Profile.email.label("doctor_email"),
            Profile.notification_prefs.label("doctor_prefs"),
        )
        .outerjoin(Patient, Consultation.patient_id == Patient.id)
        .outerjoin(Profile, Consultation.assigned_doctor_id == Profile.id)
        .where(
            Consultation.status == "scheduled",
            Consultation.scheduled_at.isnot(None),
            Consultation.scheduled_at >= now,
            Consultation.scheduled_at <= horizon,
            Consultation.reminder_sent_at.is_(None),
        )
    )
    rows = (await session.execute(due_stmt)).all()
    sent = 0
    for row in rows:
        cons = row.Consultation
        cons.reminder_sent_at = now  # idempotencia: marcar aunque no tenga email / el envío falle
        # Al paciente (siempre que tenga email).
        if row.email and await send_appointment_email(
            row.email,
            row.patient_name,
            cons.code,
            cons.scheduled_at,
            row.doctor_name,
            is_reminder=True,
        ):
            sent += 1
        # Al médico, si tiene email y no desactivó el recordatorio por correo (opt-out).
        if row.doctor_email and should_send(row.doctor_prefs, "appointment_reminder", "email"):
            subject, text = _build_doctor_reminder(row.patient_name, cons.code, cons.scheduled_at)
            if await send_mail(row.doctor_email, subject, text, category="recordatorio"):
                sent += 1
    await session.commit()
    return sent


# --- Interconsulta asíncrona (pacientes de consultorio) ---
#
# El correo de difusión sale a TODOS los médicos de una especialidad, incluidos los que nunca
# van a tomar el caso. Por eso lleva lo mínimo para decidir si vale la pena abrir el panel, y
# jamás identidad del paciente ni del médico que pide (la bandeja tampoco la muestra).
#
# Tampoco lleva el motivo ni ningún otro texto clínico (tasks/cifrado-datos-clinicos/spec.md):
# un correo sale de la API y queda en buzones, reenvíos y backups que no controlamos. El motivo
# se lee en el panel, donde la lectura se autoriza y queda en el audit_log.


def panel_url() -> str:
    """Enlace al panel médico. La base es configurable (`FRONTEND_URL`) para que un cambio de
    dominio no obligue a tocar código."""
    return f"{settings.FRONTEND_URL.rstrip('/')}/panel-medico"


def interconsultation_broadcast_email(
    specialty_name: str, age_range: str | None
) -> tuple[str, str, str]:
    """(subject, text, html) del aviso a los especialistas de que hay un caso para su
    especialidad. SIN identidad del paciente ni del médico solicitante, y SIN texto clínico."""
    edad = f"Edad: {age_range}\n\n" if age_range else ""
    edad_html = f"<p><strong>Edad:</strong> {esc(age_range)}</p>" if age_range else ""
    subject = f"Solicitud de interconsulta en {specialty_name}"
    panel = panel_url()
    text = (
        f"Un colega busca apoyo de {specialty_name}.\n\n"
        f"{edad}"
        f"Entra a tu panel para ver el caso y tomarlo si puedes ayudar:\n{panel}\n\n"
        "El primer especialista que lo tome recibe los datos de contacto del médico tratante.\n"
    )
    html = (
        f"<p>Un colega busca apoyo de <strong>{esc(specialty_name)}</strong>.</p>"
        f"{edad_html}"
        f'<p><a href="{panel}">Entra a tu panel</a> para ver el caso y tomarlo si puedes '
        "ayudar.</p>"
        "<p>El primer especialista que lo tome recibe los datos de contacto del médico "
        "tratante.</p>"
    )
    return subject, text, html


def interconsultation_taken_email(
    specialist_name: str | None, specialty_name: str
) -> tuple[str, str, str]:
    """(subject, text, html) del aviso al médico TRATANTE de que su caso fue tomado. Sin texto
    clínico: el tratante sabe de qué caso se trata desde su panel."""
    quien = specialist_name or f"Un especialista en {specialty_name}"
    subject = "Un especialista tomó tu solicitud de interconsulta"
    panel = panel_url()
    text = (
        f"{quien} tomó tu solicitud de interconsulta de {specialty_name}.\n\n"
        f"Se pondrá en contacto contigo. Sus datos también están en tu panel:\n{panel}\n"
    )
    html = (
        f"<p><strong>{esc(quien)}</strong> tomó tu solicitud de interconsulta de "
        f"{esc(specialty_name)}.</p>"
        "<p>Se pondrá en contacto contigo. Sus datos también están en "
        f'<a href="{panel}">tu panel</a>.</p>'
    )
    return subject, text, html


# --- Mensajería médico ↔ paciente (R6 de spec.md) ---
#
# Regla de oro de privacidad: NUNCA se incluye texto clínico ni nombres de adjuntos en los
# correos (tasks/cifrado-datos-clinicos/spec.md y tasks/mensajeria-medico-paciente/spec.md).
# El correo solo avisa que hay un nuevo mensaje y da un enlace seguro para entrar a la
# plataforma a responder.
#
# Anti-ráfaga (debounce): no se envía un segundo correo al mismo destinatario por el mismo
# hilo si el anterior salió hace menos de 15 min y sigue sin leer.

_LAST_MAIL_NOTIFICATIONS: dict[tuple[uuid.UUID, str], datetime] = {}


def record_mail_debounce(consultation_id: uuid.UUID, recipient_role: str) -> None:
    _LAST_MAIL_NOTIFICATIONS[(consultation_id, recipient_role)] = datetime.now(UTC)


def clear_mail_debounce(consultation_id: uuid.UUID, recipient_role: str) -> None:
    _LAST_MAIL_NOTIFICATIONS.pop((consultation_id, recipient_role), None)


async def should_debounce_mail(
    session: AsyncSession, consultation_id: uuid.UUID, direction: str
) -> bool:
    """Verifica si debe aplicarse anti-ráfaga (debounce) para este envío."""
    recipient_role = "doctor" if direction == "patient_to_doctor" else "patient"
    now = datetime.now(UTC)
    debounce_limit = timedelta(minutes=settings.MESSAGING_MAIL_DEBOUNCE_MINUTES)

    # 1. Comprobación en memoria
    last_sent = _LAST_MAIL_NOTIFICATIONS.get((consultation_id, recipient_role))
    if last_sent:
        if (now - last_sent) < debounce_limit:
            # Hay notificación reciente en memoria: revisar si aún hay no leídos
            unread_count = await session.scalar(
                select(func.count(Message.id)).where(
                    Message.consultation_id == consultation_id,
                    Message.direction == direction,
                    Message.read_at.is_(None),
                )
            )
            if unread_count and unread_count > 0:
                return True
        else:
            _LAST_MAIL_NOTIFICATIONS.pop((consultation_id, recipient_role), None)

    # 2. Comprobación en base de datos (resiliente tras reinicios)
    recent_unread = await session.scalar(
        select(func.count(Message.id)).where(
            Message.consultation_id == consultation_id,
            Message.direction == direction,
            Message.read_at.is_(None),
            Message.sent_at >= now - debounce_limit,
        )
    )
    if recent_unread and recent_unread >= 2:
        return True

    return False


def doctor_message_received_email(
    consultation_id: uuid.UUID, code: str | None
) -> tuple[str, str, str]:
    """(subject, text, html) para el médico cuando el paciente le escribe.

    SIN NINGÚN TEXTO CLÍNICO.
    """
    subject = "Tu paciente te escribió"
    codigo_text = f"Código de caso: {code}\n\n" if code else ""
    codigo_html = (
        f'<p style="color:{mail_layout.MUTED};font-size:14px;">'
        f"<strong>Código de caso:</strong> {esc(code)}</p>"
        if code
        else ""
    )
    url = f"{settings.FRONTEND_URL.rstrip('/')}/panel-medico/consulta/{consultation_id}"
    text = (
        "Tienes un nuevo mensaje de tu paciente en la plataforma.\n\n"
        f"{codigo_text}"
        f"Ingresa a tu panel para ver la consulta y responder:\n{url}\n"
    )
    escaped_url = esc(url)
    btn = mail_layout.button(escaped_url, "Ver consulta y responder")
    html = (
        "<p>Tienes un nuevo mensaje de tu paciente en la plataforma.</p>"
        f"{codigo_html}"
        f'<p style="margin:26px 0;">{btn}</p>'
        f'<p style="color:{mail_layout.MUTED};font-size:13px;word-break:break-all;">'
        f"Si el botón no funciona, copia este enlace: "
        f'<a href="{escaped_url}" style="color:{mail_layout.BLUE};">{escaped_url}</a></p>'
    )
    return subject, text, html


def patient_message_received_email(
    consultation_id: uuid.UUID, code: str | None, has_account: bool = False
) -> tuple[str, str, str]:
    """(subject, text, html) para el paciente cuando el médico le responde.

    SIN NINGÚN TEXTO CLÍNICO.
    """
    subject = "Tu médico te respondió"
    codigo_text = f"Código de caso: {code}\n\n" if code else ""
    codigo_html = (
        f'<p style="color:{mail_layout.MUTED};font-size:14px;">'
        f"<strong>Código de caso:</strong> {esc(code)}</p>"
        if code
        else ""
    )
    if has_account:
        url = f"{settings.FRONTEND_URL.rstrip('/')}/mi-caso"
    else:
        url = build_waiting_room_url(consultation_id)

    text = (
        "Tu médico te ha enviado un mensaje en la plataforma.\n\n"
        f"{codigo_text}"
        f"Ingresa para ver el mensaje y continuar con tu atención:\n{url}\n\n"
        "Si tu situación empeora o hay señales de alarma, busca atención presencial urgente.\n"
    )
    escaped_url = esc(url)
    btn = mail_layout.button(escaped_url, "Ver respuesta de mi médico")
    html = (
        "<p>Tu médico te ha enviado un mensaje en la plataforma.</p>"
        f"{codigo_html}"
        f'<p style="margin:26px 0;">{btn}</p>'
        f'<p style="color:{mail_layout.MUTED};font-size:13px;word-break:break-all;">'
        f"Si el botón no funciona, copia este enlace: "
        f'<a href="{escaped_url}" style="color:{mail_layout.BLUE};">{escaped_url}</a></p>'
        f'<p style="background:#fff7ed;border-left:4px solid #f59e0b;padding:12px 14px;'
        f'margin:22px 0 0;font-size:14px;">'
        "Si tu situación empeora o hay señales de alarma, busca atención presencial urgente.</p>"
    )
    return subject, text, html


async def message_received_mail_args(
    session: AsyncSession, consultation_id: uuid.UUID, message_id: uuid.UUID, direction: str
) -> dict | None:
    """Prepara los argumentos para enviar el correo de aviso de mensaje nuevo.

    Retorna None si no corresponde enviar (sin correo, opt-out, o debounce activo).
    """
    if await should_debounce_mail(session, consultation_id, direction):
        logger.info(
            "MAIL:debounce_skip consultation_id=%s direction=%s", consultation_id, direction
        )
        return None

    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        return None

    if direction == "patient_to_doctor":
        if not consultation.assigned_doctor_id:
            return None
        doctor = await session.get(Profile, consultation.assigned_doctor_id)
        if doctor is None or not doctor.email:
            return None
        if not should_send(doctor.notification_prefs, "message_received", "email"):
            return None
        record_mail_debounce(consultation_id, "doctor")
        subject, text, html = doctor_message_received_email(consultation.id, consultation.code)
        return {
            "to_email": doctor.email,
            "subject": subject,
            "text": text,
            "html": html,
            "category": "mensaje",
        }

    if direction == "doctor_to_patient":
        patient = await session.get(Patient, consultation.patient_id)
        if patient is None or not patient.email:
            return None
        record_mail_debounce(consultation_id, "patient")
        has_account = patient.user_id is not None
        subject, text, html = patient_message_received_email(
            consultation.id, consultation.code, has_account=has_account
        )
        return {
            "to_email": patient.email,
            "subject": subject,
            "text": text,
            "html": html,
            "category": "mensaje",
        }

    return None


@best_effort
async def send_message_notification(
    to_email: str,
    subject: str,
    text: str,
    html: str,
    category: str = "mensaje",
) -> bool:
    """Envía el aviso de mensaje nuevo. Best-effort (nunca propaga excepción)."""
    return await send_mail(to_email, subject, text, html=html, category=category)
