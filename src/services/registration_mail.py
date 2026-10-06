"""Correos de alta: paciente que entra a la cola y médico que se registra o es aprobado.

Cinco correos, dos direcciones:

| # | Cuándo | A quién |
|---|---|---|
| A | Entra un paciente a la cola pública | operación (`MAIL_INTERNAL_RECIPIENTS`) |
| B | Un médico se registra y el SACS/FPV NO lo valida | operación |
| C | Un médico se registra y el SACS/FPV SÍ lo valida | operación |
| D | Un médico se registra y el SACS/FPV NO lo valida | el médico (le pide los papeles) |
| E | Un médico queda habilitado (por el registro oficial o por un admin) | el médico |

**Módulo aparte de `notifications.py` a propósito.** Ese fichero es el de las preferencias del
médico: un catálogo opt-out donde el destinatario puede apagar lo que no quiere. Estos correos
son de ciclo de vida de la cuenta —"tu registro fue rechazado", "ya puedes entrar"— y no se
pueden apagar. Meterlos allí invitaría al siguiente lector a añadirlos al catálogo y volverlos
opcionables por error.

Tres capas, la misma forma que `notifications.py`:

1. `_build_*` — puras: reciben valores planos y devuelven `(asunto, texto, html)`. Sin sesión y
   sin IO, así que son las que se prueban a fondo (incluida la aserción negativa de PII).
2. `*_mail_args` — resuelven con la **sesión viva** y devuelven valores planos. `None` significa
   "no hay a quién escribir", que no es un error. Existen porque el `BackgroundTask` corre
   DESPUÉS de cerrar la request y allí ya no se puede consultar la base.
3. `send_*` — llaman a `mail.send_mail`, best-effort: un fallo de correo nunca rompe el alta.

⚠️ **Frontera de PII (decisión explícita, ver `tasks/correos-de-alta/spec.md`).** Los buzones de
operación incluyen un Gmail personal: lo que entre en estos correos queda fuera de la
plataforma y fuera del `audit_log`. Del paciente van nombre, teléfono, zona y especialidad —lo
que permite contactarlo— y **nunca** su cédula, sus alergias ni la descripción del caso. Del
médico sí va la cédula: es el asunto mismo del aviso. Hay un test que lo comprueba por la
negativa; si añades un campo al correo A, ese test debe seguir pasando.
"""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.tz import to_local
from src.models.consultation import Consultation
from src.models.doctor import Doctor
from src.models.patient import Patient
from src.models.professional_type import ProfessionalType
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.services.mail import best_effort, esc, send_mail

logger = logging.getLogger("mpv.api")


# Documentos que el médico no verificado debe enviar de vuelta. Lista única: la usan el texto y
# el HTML del correo D, y el test exige que los tres aparezcan.
REQUIRED_DOCUMENTS = (
    "Título de médico",
    "Licencia del SACS",
    "Carta de artículo 8",
)

# Motivo técnico -> lo que se le dice a una persona. Fuente ÚNICA de los motivos: el test
# recorre estas claves y exige que cada una produzca un texto distinto, así que añadir un motivo
# sin su frase rompe la suite en vez de producir un correo mudo.
DOCTOR_REJECTION_REASONS: dict[str, str] = {
    "sin_tipo": (
        "No indicaste tu tipo de profesional, así que no pudimos saber en qué registro "
        "oficial verificarte."
    ),
    "tipo_no_verificable": (
        "Tu profesión no se verifica en línea todavía, así que tu registro lo revisa una "
        "persona de nuestro equipo."
    ),
    "no_encontrado": (
        "No encontramos tu cédula en el registro oficial. Revisa más abajo con qué cédula "
        "quedaste registrado: si tiene algún error, escríbenoslo en tu respuesta."
    ),
    "datos_incompletos": (
        "El registro oficial respondió sin tu número de licencia, así que hay que verificarla "
        "a mano."
    ),
    "servicio_no_disponible": (
        "El registro oficial no respondió cuando intentamos verificarte. No es un problema "
        "con tus datos: lo revisamos a mano."
    ),
}

_FALLBACK_REASON = "No pudimos verificar tu credencial automáticamente."


def reason_text(reason: str | None) -> str:
    """Frase para una persona a partir del motivo técnico. Nunca devuelve vacío: un motivo
    desconocido (uno nuevo sin su frase) cae a un texto genérico en vez de dejar el correo
    sin explicación."""
    return DOCTOR_REJECTION_REASONS.get(reason or "", _FALLBACK_REASON)


def _admin_case_url() -> str:
    """Enlace al panel de operación. Los destinatarios de A son operación, no el médico que
    atiende, así que apunta a la lista de casos del admin y no al panel médico."""
    return f"{settings.FRONTEND_URL.rstrip('/')}/admin/pacientes"


def _doctor_panel_url() -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/panel-medico"


def _rows_text(rows: list[tuple[str, str | None]]) -> str:
    """Pares etiqueta/valor como texto plano, saltando los vacíos."""
    return "\n".join(f"{label}: {value}" for label, value in rows if value)


def _rows_html(rows: list[tuple[str, str | None]]) -> str:
    """Los mismos pares en HTML. Saltar los vacíos evita filas como 'Especialidad: None'."""
    return "<br>".join(
        f"<strong>{esc(label)}:</strong> {esc(value)}" for label, value in rows if value
    )


# --- A: paciente nuevo en la cola pública -------------------------------------


def _build_new_patient(
    patient_name: str,
    phone: str | None,
    zone: str | None,
    specialty: str | None,
    code: str,
) -> tuple[str, str, str]:
    """Aviso de operación: entró un paciente a la cola.

    Lleva el teléfono a propósito: el objetivo del correo es que se le pueda contactar SIN
    entrar al panel. Un aviso que solo dijera "entró alguien, entra a mirar" añadiría un paso
    en vez de quitarlo.
    """
    subject = f"Nuevo paciente en la cola: {patient_name}"
    rows: list[tuple[str, str | None]] = [
        ("Paciente", patient_name),
        ("WhatsApp", phone),
        ("Zona", zone),
        ("Especialidad solicitada", specialty),
        ("Código del caso", code),
    ]
    text = (
        "Entró un paciente nuevo a la cola pública.\n\n"
        f"{_rows_text(rows)}\n\n"
        f"Ver el caso: {_admin_case_url()}\n"
    )
    html = (
        "<p>Entró un paciente nuevo a la cola pública.</p>"
        f"<p>{_rows_html(rows)}</p>"
        f'<p><a href="{_admin_case_url()}">Ver el caso en el panel</a></p>'
    )
    return subject, text, html


async def new_patient_mail_args(session: AsyncSession, consultation: Consultation) -> dict | None:
    """Args del aviso A, o `None` si este caso no debe avisarse.

    Tres motivos para no avisar, y ninguno es un fallo:
    - no hay buzones de operación configurados;
    - el paciente es **de consultorio**: es privado de su médico y la plataforma no lo
      comparte (mismo criterio que `patients.list_patients`);
    - la consulta viene **agendada**: esa ya tiene su propio correo por `notifications`, y
      además una cita futura no es la urgencia que este aviso persigue.
    """
    if not settings.internal_mail_recipients:
        return None
    if consultation.scheduled_at is not None:
        return None
    patient = await session.get(Patient, consultation.patient_id)
    if patient is None or patient.created_by_doctor_id is not None:
        return None
    specialty = None
    if consultation.specialty_id is not None:
        specialty = await session.scalar(
            select(Specialty.name).where(Specialty.id == consultation.specialty_id)
        )
    return {
        "patient_name": patient.full_name,
        "phone": patient.phone_whatsapp,
        "zone": patient.affected_zone,
        "specialty": specialty,
        "code": consultation.code,
    }


@best_effort
async def send_new_patient_alert(**kwargs) -> bool:
    """Envía el aviso A a los buzones de operación. Best-effort."""
    subject, text, html = _build_new_patient(**kwargs)
    return await _send_internal(subject, text, html, category="alta-paciente")


# --- B / C: registro de médico, hacia operación -------------------------------


def _build_doctor_registered(
    full_name: str,
    cedula: str | None,
    email: str | None,
    phone: str | None,
    professional_type: str | None,
    specialty: str | None,
    registered_at: datetime | None,
    verified: bool,
    reason: str | None,
) -> tuple[str, str, str]:
    """Aviso de operación: se registró un médico (B si no validó, C si sí).

    Un solo constructor para los dos porque el cuerpo es el mismo expediente; lo que cambia es
    el veredicto y el motivo. Separarlos duplicaría la lista de campos, que es justo lo que se
    querría mantener sincronizado.

    La **cédula va**, al revés que en el correo del paciente: aquí es el asunto del aviso. Sin
    ella nadie puede cotejar el título que llegue por respuesta.
    """
    estado = "verificado por el registro oficial" if verified else "SIN verificar"
    subject = f"Nuevo médico registrado ({'verificado' if verified else 'pendiente'}): {full_name}"
    rows: list[tuple[str, str | None]] = [
        ("Nombre", full_name),
        ("Cédula", cedula),
        ("Email", email),
        ("Teléfono", phone),
        ("Tipo profesional", professional_type),
        ("Especialidad", specialty),
        ("Estado", estado),
    ]
    if not verified:
        rows.append(("Motivo", reason_text(reason)))
    if registered_at is not None:
        rows.append(("Registrado", f"{to_local(registered_at):%d/%m/%Y %H:%M} (Venezuela)"))
    cierre = (
        "Ya puede atender."
        if verified
        else (
            "No puede atender todavía. Se le pidió por correo que responda con su título, su "
            "licencia del SACS y su carta de artículo 8."
        )
    )
    text = f"Se registró un médico.\n\n{_rows_text(rows)}\n\n{cierre}\n"
    html = f"<p>Se registró un médico.</p><p>{_rows_html(rows)}</p><p>{cierre}</p>"
    return subject, text, html


async def doctor_registered_mail_args(session: AsyncSession, doctor: Doctor) -> dict | None:
    """Args del aviso B/C, o `None` si no hay buzones de operación configurados."""
    if not settings.internal_mail_recipients:
        return None
    professional_type = None
    if doctor.professional_type_id is not None:
        professional_type = await session.scalar(
            select(ProfessionalType.name).where(ProfessionalType.id == doctor.professional_type_id)
        )
    specialty = None
    if doctor.specialty_id is not None:
        specialty = await session.scalar(
            select(Specialty.name).where(Specialty.id == doctor.specialty_id)
        )
    return {
        "full_name": doctor.full_name,
        "cedula": doctor.cedula,
        "email": doctor.email,
        "phone": doctor.phone,
        "professional_type": professional_type,
        "specialty": specialty,
        "registered_at": doctor.created_at,
        "verified": doctor.verified,
    }


@best_effort
async def send_doctor_registered_alert(reason: str | None = None, **kwargs) -> bool:
    """Envía el aviso B/C a los buzones de operación. Best-effort."""
    subject, text, html = _build_doctor_registered(reason=reason, **kwargs)
    return await _send_internal(subject, text, html, category="alta-medico")


# --- D: al médico que no quedó verificado -------------------------------------


def _build_doctor_rejected(
    full_name: str, cedula: str | None, reason: str | None
) -> tuple[str, str, str]:
    """Correo al médico cuya credencial no validó: qué pasó y qué tiene que mandar.

    Le devolvemos **su propia cédula** porque teclearla mal es la causa más común de que el
    registro oficial no lo encuentre, y sin verla escrita no tiene forma de darse cuenta.

    Las direcciones salen de `MAIL_INTERNAL_RECIPIENTS`, las mismas que reciben el aviso
    interno: si operación cambia de buzón, este texto cambia con ella. Si no hay ninguna
    configurada cae a `CONTACT_EMAIL`, **nunca** al remitente: el remitente es `no-reply@`, y
    mandar ahí un título de médico es tirarlo.

    Dice "escríbenos a X" y no "responde a este correo" a propósito: una respuesta iría al
    `From`, que es ese mismo `no-reply@`. La instrucción tiene que describir lo que de verdad
    funciona, no lo que suena natural.
    """
    destinos = ", ".join(settings.internal_mail_recipients) or settings.CONTACT_EMAIL
    documentos_text = "\n".join(f"  - {d}" for d in REQUIRED_DOCUMENTS)
    documentos_html = "".join(f"<li>{d}</li>" for d in REQUIRED_DOCUMENTS)
    subject = "Tu registro en Médicos por Venezuela necesita verificación"
    cedula_line = f"Cédula con la que quedaste registrado: {cedula}" if cedula else ""
    text = (
        f"Hola {full_name},\n\n"
        "Recibimos tu registro, pero todavía no podemos habilitarte para atender.\n\n"
        f"{reason_text(reason)}\n\n"
        f"{cedula_line}\n\n"
        f"Para completar tu verificación, escríbenos a {destinos} adjuntando:\n"
        f"{documentos_text}\n\n"
        "En cuanto revisemos los documentos te avisamos por este mismo medio.\n"
    )
    cedula_html = (
        f"<p><strong>Cédula con la que quedaste registrado:</strong> {esc(cedula)}</p>"
        if cedula
        else ""
    )
    html = (
        f"<p>Hola {esc(full_name)},</p>"
        "<p>Recibimos tu registro, pero todavía no podemos habilitarte para atender.</p>"
        f"<p>{reason_text(reason)}</p>"
        f"{cedula_html}"
        f"<p>Para completar tu verificación, escríbenos a <strong>{esc(destinos)}</strong> "
        "adjuntando:</p>"
        f"<ul>{documentos_html}</ul>"
        "<p>En cuanto revisemos los documentos te avisamos por este mismo medio.</p>"
    )
    return subject, text, html


@best_effort
async def send_doctor_rejected_email(
    to_email: str, full_name: str, cedula: str | None, reason: str | None
) -> bool:
    """Envía el correo D al médico. Best-effort."""
    subject, text, html = _build_doctor_rejected(full_name, cedula, reason)
    return await send_mail(to_email, subject, text, html, category="registro-medico")


# --- E: al médico que quedó habilitado ----------------------------------------


def _build_doctor_approved(full_name: str) -> tuple[str, str, str]:
    """Correo al médico habilitado. Sale tanto del registro automático como del clic del
    admin: para el médico es el mismo hecho, y quién apretó el botón no le aporta nada."""
    subject = "Tu registro en Médicos por Venezuela fue aprobado"
    text = (
        f"Hola {full_name},\n\n"
        "Tu registro fue aprobado y tu cuenta ya está habilitada para atender pacientes.\n\n"
        f"Entra al panel médico: {_doctor_panel_url()}\n\n"
        "Gracias por sumarte.\n"
    )
    html = (
        f"<p>Hola {esc(full_name)},</p>"
        "<p>Tu registro fue aprobado y tu cuenta ya está habilitada para atender pacientes.</p>"
        f'<p><a href="{_doctor_panel_url()}">Entrar al panel médico</a></p>'
        "<p>Gracias por sumarte.</p>"
    )
    return subject, text, html


async def doctor_email(session: AsyncSession, doctor: Doctor) -> str | None:
    """A dónde se le escribe a un médico, o `None` si no hay dónde.

    `doctors.email` -> `users.email` (vía `user_id`): las fichas backfilleadas nacieron sin
    correo porque el contacto vivía en la cuenta, y son justo las antiguas que un admin puede
    estar aprobando ahora.

    Definición ÚNICA, usada por los dos correos al médico. Tenerla dos veces era una bomba de
    relojería: hoy `DoctorCreate` exige email, así que leer `doctor.email` a secas funciona en
    el registro; el día que aparezca un alta de médico sin email, el correo de rechazo dejaría
    de salir en silencio mientras el de aprobación seguiría funcionando, y la diferencia sería
    invisible.
    """
    if doctor.email:
        return doctor.email
    if doctor.user_id is None:
        return None
    return await session.scalar(select(Profile.email).where(Profile.id == doctor.user_id))


async def doctor_approved_mail_args(session: AsyncSession, doctor: Doctor) -> dict | None:
    """Args del correo E, o `None` si no hay a dónde escribirle.

    Que no haya correo no es un error: aprobar a un médico sin dirección es una acción válida,
    solo que no tiene a dónde llegar. Queda un warning para que no sea un silencio total.
    """
    to_email = await doctor_email(session, doctor)
    if not to_email:
        # Sin PII en el log: el id de la ficha basta para encontrarla en el panel.
        logger.warning("MAIL:skip reason=doctor_sin_email doctor_id=%s", doctor.id)
        return None
    return {"to_email": to_email, "full_name": doctor.full_name}


@best_effort
async def send_doctor_approved_email(to_email: str, full_name: str) -> bool:
    """Envía el correo E al médico. Best-effort."""
    subject, text, html = _build_doctor_approved(full_name)
    return await send_mail(to_email, subject, text, html, category="registro-medico")


# --- Envío a los buzones de operación -----------------------------------------


async def _send_internal(subject: str, text: str, html: str, *, category: str) -> bool:
    """Manda un aviso a `MAIL_INTERNAL_RECIPIENTS`.

    El primer buzón va en `to` y el resto en `bcc`: son destinatarios paralelos del mismo
    aviso, y ponerlos todos en `to` haría que cada uno viera las direcciones de los demás sin
    ninguna razón. Devuelve False si no hay ninguno configurado.
    """
    recipients = settings.internal_mail_recipients
    if not recipients:
        return False
    return await send_mail(
        recipients[0],
        subject,
        text,
        html,
        category=category,
        bcc=recipients[1:] or None,
    )


# --- Verificación de correo (código OTP de 6 dígitos) ---------------------------
#
# El endpoint POST /email-verification/send llama a `build_email_verification_mail`
# para obtener el asunto/texto/html y luego a `send_email_verification_email`
# para enviarlo. `debug_code` se maneja en el router (solo dev/e2e).


def build_email_verification_mail(code: str, ttl_minutes: int) -> tuple[str, str, str]:
    """Construye el correo con el código de verificación de 6 dígitos.

    Subject: "Tu código de verificación es {code}"
    HTML: código GRANDE (≈34px, font-weight:800, letter-spacing:6px),
          "Válido por {ttl} minutos",
          nota de revisar spam y de que el paso es necesario para verificar el correo.
    """
    subject = f"Tu código de verificación es {code}"
    text = (
        f"Tu código de verificación es: {code}\n\n"
        f"Válido por {ttl_minutes} minutos.\n\n"
        "Este código es necesario para verificar tu correo y completar el registro.\n"
        "Si no lo pediste, ignora este correo.\n\n"
        "Nota: revisa tu carpeta de spam si no lo ves en la bandeja de entrada."
    )
    html = (
        f"<p>Tu código de verificación es:</p>"
        f'<p style="font-size:34px;font-weight:800;letter-spacing:6px;'
        f"font-family:'Nunito Sans',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
        f'color:#18202b;text-align:center;">{code}</p>'
        f"<p>Válido por {ttl_minutes} minutos.</p>"
        "<p>Este código es necesario para verificar tu correo y completar el registro.</p>"
        "<p>Si no lo pediste, ignora este correo.</p>"
        "<p><small>Nota: revisa tu carpeta de spam si no lo ves en la "
        "bandeja de entrada.</small></p>"
    )
    return subject, text, html


@best_effort
async def send_email_verification_email(to_email: str, code: str, ttl_minutes: int) -> bool:
    """Envía el correo de verificación al usuario. Best-effort.

    Usa la categoría "email-verification" para Mailtrap.
    """
    subject, text, html = build_email_verification_mail(code, ttl_minutes)
    return await send_mail(to_email, subject, text, html, category="email-verification")
