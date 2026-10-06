"""Configuración de la aplicación cargada desde variables de entorno.

Desarrollo: por defecto apunta a un Postgres LOCAL (servicio `db` de docker-compose).
Producción: se define `DATABASE_URL` (o las piezas POSTGRES_*) apuntando a Supabase.

El driver es asíncrono (asyncpg), así que la URL usa el esquema postgresql+asyncpg://.
"""

import ssl
from datetime import date
from functools import lru_cache
from typing import Any
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

# Modos de SSL (asyncpg no entiende "sslmode" en la URL; el cifrado va por
# connect_args, ver db/session.py). Semántica de Postgres:
#   - require            -> cifra pero NO verifica la CA (verify_mode=CERT_NONE).
#   - verify-ca/-full    -> cifra Y verifica la CA (requiere la CA en el trust store).
# Supabase usa una CA self-signed: con verify-full falla ("self-signed certificate
# in certificate chain"); por eso 'require' debe cifrar sin verificar.
_SSL_NO_VERIFY = {"require"}
_SSL_VERIFY = {"verify-ca", "verify-full"}
_SSL_REQUIRED = _SSL_NO_VERIFY | _SSL_VERIFY


class Settings(BaseSettings):
    """Lee la configuración desde el entorno (o un archivo .env)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Metadatos de la API ---
    PROJECT_NAME: str = "API Médicos por Venezuela"
    API_V1_PREFIX: str = "/api/v1"
    ENVIRONMENT: str = "development"

    # --- Base de datos ---
    # DATABASE_URL tiene prioridad. Si no se define, se arma desde las piezas POSTGRES_*.
    # Los valores por defecto apuntan al Postgres local de docker-compose.
    DATABASE_URL: str | None = None
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "medicos"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "localdev"
    POSTGRES_SSLMODE: str = "prefer"

    # Pooler en modo transaction (Supabase, puerto 6543) requiere desactivar el
    # caché de prepared statements de asyncpg. Es inocuo en local.
    DB_DISABLE_PREPARED_STATEMENTS: bool = True

    # --- Autenticación (JWT de Supabase) ---
    # Secreto JWT del proyecto Supabase (Project Settings -> API -> JWT Secret).
    # En producción es OBLIGATORIO definirlo por entorno. El valor por defecto es solo
    # para desarrollo/pruebas locales y NO debe usarse en producción.
    SUPABASE_JWT_SECRET: str = "dev-insecure-jwt-secret-change-me"
    SUPABASE_JWT_ALGORITHM: str = "HS256"
    SUPABASE_JWT_AUDIENCE: str = "authenticated"
    # Emisor esperado del token ({SUPABASE_URL}/auth/v1 en prod). PyJWT solo verifica `iss`
    # si se le pasa el parámetro, así que dejarlo en None mantiene dev intacto (el Supabase
    # local emite otro iss). En producción conviene definirlo por entorno.
    SUPABASE_JWT_ISSUER: str | None = None
    # Opcional: URL de JWKS (claves asimétricas ES256/RS256 "JWT signing keys" de Supabase;
    # el CLI de Supabase local las usa por defecto: {API_URL}/auth/v1/.well-known/jwks.json).
    # Si no se define, solo se valida HS256 con SUPABASE_JWT_SECRET (comportamiento de siempre).
    SUPABASE_JWKS_URL: str | None = None

    # --- Supabase Admin API (usuarios de Auth) y Storage (adjuntos del chat) ---
    # Base URL del proyecto Supabase (local: el gateway del CLI; prod: el proyecto real).
    # El service-role key da acceso admin total (bypassa RLS): NUNCA se loguea y solo lo usan
    # `src/services/users.py` (crear usuarios de Auth) y `src/services/storage.py` (leer y
    # escribir en el bucket PRIVADO de adjuntos clínicos: con el anon key no se puede).
    # En producción es OBLIGATORIO definirlo por entorno; el valor por defecto es solo para
    # desarrollo/pruebas locales.
    SUPABASE_URL: str = "http://127.0.0.1:54321"
    SUPABASE_SERVICE_ROLE_KEY: str = "dev-insecure-service-role-key-change-me"
    # Base de la API de Storage. Vacía = se deriva de SUPABASE_URL ({SUPABASE_URL}/storage/v1).
    # Solo hace falta definirla si Storage vive detrás de otro host/gateway.
    SUPABASE_STORAGE_URL: str | None = None

    # --- Token de acceso a la sala del paciente anónimo (hallazgo M3) ---
    # Secreto PROPIO, distinto del de Supabase a propósito (ver src/core/consultation_token.py).
    # En producción es OBLIGATORIO definirlo por entorno; el default solo sirve en local.
    CONSULTATION_TOKEN_SECRET: str = "dev-insecure-consultation-token-secret-change-me"
    # 24 h: cubre de sobra la espera real (incluida una cola que quede de un día para otro) y
    # convierte una URL filtrada en algo inservible al día siguiente. Más corto arriesga dejar
    # fuera de su propia sala a un paciente anónimo, que no tiene forma de re-emitir el token.
    CONSULTATION_TOKEN_TTL_HOURS: int = 24
    # ponytail: interruptor de cutover, no una opción permanente. El backend y el frontend se
    # despliegan por separado (EC2 y Amplify), así que exigir el token de golpe deja sin sala a
    # los pacientes durante la ventana entre ambos deploys. Secuencia: desplegar el backend con
    # esto en `false` -> desplegar el frontend que ya manda el token -> ponerlo en `true`.
    # Techo conocido: mientras esté en `false`, M3 NO está cerrado (basta el id, como antes).
    # Borrar esta bandera y el `if` de require_consultation_token una vez hecho el cutover.
    CONSULTATION_TOKEN_REQUIRED: bool = True

    # --- Cifrado de datos clínicos (ver src/core/clinical_crypto.py) ---
    # AES-256-GCM, 32 bytes en base64. Vive SOLO aquí: ni en Supabase ni en el frontend. Sin
    # ella no hay forma de leer motivos ni notas de la base, así que se custodia como el
    # service_role y su pérdida es pérdida de datos (ver docs/cifrado-datos-clinicos.md).
    # El default es público (está en el repo) y solo sirve en local: el arranque en producción
    # lo rechaza. Generar una: `uv run python scripts/encrypt_clinical_data.py --generate-key`.
    CLINICAL_DATA_ENCRYPTION_KEY: str = "ZGV2LWluc2VjdXJlLWNsaW5pY2FsLWtleS0zMmJ5dGU="
    # Claves anteriores (coma-separadas), solo para DESCIFRAR durante una rotación. Se vacía
    # cuando el script de backfill ya re-cifró todo con la activa.
    CLINICAL_DATA_ENCRYPTION_PREVIOUS_KEYS: str = ""

    # --- Resiliencia de la cola ---
    # Minutos tras los cuales una consulta 'in_progress' sin cerrar se considera
    # estancada y se devuelve a 'waiting' (la libera para otro médico).
    STALE_CONSULTATION_MINUTES: int = 30

    # --- Sala de espera en vivo (SSE, ver services/waiting_room.py) ---
    # Cada cuánto el stream relee el estado del caso. Es una lectura por PK por paciente con la
    # sala abierta: 4 s hace que el botón aparezca casi al instante sin cargar la base.
    WAITING_ROOM_POLL_SECONDS: float = 4.0
    # Latido si no hubo eventos: por debajo del timeout de inactividad de cualquier proxy.
    WAITING_ROOM_HEARTBEAT_SECONDS: float = 15.0
    # Vida máxima de un stream; el cliente reconecta. Acota conexiones colgadas.
    WAITING_ROOM_STREAM_MAX_SECONDS: float = 300.0

    # --- Mensajería médico ↔ paciente ---
    # Ventana tras cerrar el caso en la que el hilo sigue admitiendo mensajes (CA2.2/CA4.5).
    MESSAGING_AFTER_CLOSE_HOURS: int = 72
    # Anti-ráfaga de los correos de aviso: no se manda un segundo aviso al mismo destinatario
    # por el mismo hilo antes de esto si el anterior sigue sin leer (CA6.3).
    MESSAGING_MAIL_DEBOUNCE_MINUTES: int = 15
    # Tope de mensajes del paciente por hilo y hora (CA4.4), ADEMÁS del límite por IP
    # (PUBLIC_WRITE_RATE_LIMIT) de las rutas de escritura.
    MESSAGING_PATIENT_HOURLY_LIMIT: int = 30
    # Longitud máxima del cuerpo de un mensaje (CA2.3). El esquema `MessageCreate` la lee de
    # aquí, así que es el único sitio donde se cambia (y el que ve el OpenAPI).
    MESSAGING_MAX_BODY_CHARS: int = 2000
    # Cuánto vale una señal de actividad del paciente para considerarlo "en línea" en el buzón
    # del médico (CA7.1). El registro es EN MEMORIA DE PROCESO: válido para el despliegue
    # actual de un solo uvicorn; con varias réplicas cada una vería su propia mitad.
    MESSAGING_PATIENT_PRESENCE_TTL_SECONDS: int = 45
    MESSAGING_MAX_ATTACHMENT_SIZE_BYTES: int = 10485760  # 10 MB
    MESSAGING_ALLOWED_ATTACHMENT_MIME_TYPES: str = (
        "application/pdf,image/jpeg,image/png,image/webp"
    )
    # Bucket PRIVADO de los adjuntos clínicos del chat (ver services/storage.py). Hay que
    # crearlo en el proyecto de Supabase; en local lo declara `supabase/config.toml`.
    STORAGE_BUCKET_ATTACHMENTS: str = "chat-attachments"

    # --- Videoconsulta (Jitsi) ---
    # Instancia self-hosted (salas abiertas, sin moderador). NO se usa el público meet.jit.si por
    # defecto porque ahora exige login de moderador ("no moderators have yet arrived"). Override
    # con la env JITSI_DOMAIN si el host cambia.
    JITSI_DOMAIN: str = "meet.medicosporvenezuela.org"

    # --- Correo (Mailtrap) — base para recordatorios y alertas ---
    # Sin token, el envío queda DESHABILITADO (no-op con warning): local y tests no envían
    # nada por accidente. En prod va en .env.production (Mailtrap → Sending → API Tokens).
    MAILTRAP_API_TOKEN: str = ""
    MAIL_FROM_EMAIL: str = "no-reply@medicosporvenezuela.org"
    MAIL_FROM_NAME: str = "Médicos por Venezuela"
    # Base para los enlaces de los correos. El apex es el host canónico (www quedó con fallo de
    # TLS). Sin barra final: los constructores de enlaces la ponen.
    FRONTEND_URL: str = "https://medicosporvenezuela.org"
    # Sandbox (Email Testing): si se define el inbox, entrega ahí en vez de enviar de verdad —
    # ideal para probar plantillas en dev sin spamear correos reales.
    MAILTRAP_INBOX_ID: str | None = None
    # Difusión (fan-out de interconsultas). Una especialidad puede tener cientos de médicos:
    # un correo por cabeza serían cientos de peticiones al stream transaccional. Se manda por
    # el stream BULK de Mailtrap, en lotes por BCC.
    MAIL_BULK_BATCH_SIZE: int = 50
    # Tope duro de destinatarios por difusión. Si se supera, se notifica hasta el tope y se
    # LOGUEA el recorte: un truncamiento silencioso se leería como "se notificó a todos".
    MAIL_FANOUT_MAX: int = 500
    # Buzones de OPERACIÓN que reciben los avisos de alta (paciente nuevo, médico registrado):
    # lista separada por comas. VACÍA por defecto, a propósito y por el mismo criterio que
    # MAILTRAP_API_TOKEN: con las direcciones reales cableadas aquí, cualquier entorno de
    # pruebas que tenga token de Mailtrap le escribiría de verdad a esas personas en el primer
    # registro de prueba. Se define solo en .env.production — si falta allí, no sale ningún
    # aviso interno (ese es el precio consciente de equivocarse hacia el lado callado).
    MAIL_INTERNAL_RECIPIENTS: str = ""
    # Dirección PÚBLICA de contacto de la organización: la que se le da a un médico para que
    # mande sus documentos. Es otra cosa que MAIL_INTERNAL_RECIPIENTS —esa es "a quién avisamos"
    # e incluye buzones personales; esta es "a dónde escribe la gente"— y por eso sí trae valor
    # por defecto: si quedara vacía, el correo le pediría al médico que mandara su título a una
    # dirección no-reply, o sea a la basura.
    CONTACT_EMAIL: str = "info@medicosporvenezuela.org"
    # Logotipo del banner de todos los correos (ver services/mail_layout.py). PNG y no el SVG
    # del sitio: Gmail, Outlook y Yahoo descartan un <img> que apunte a un SVG. Absoluta y
    # apuntando a PRODUCCIÓN por defecto, en vez de derivarse de FRONTEND_URL: en local esa
    # base es localhost, y un correo enviado desde una máquina de desarrollo —que con token de
    # Mailtrap sale de verdad— llegaría con el logotipo roto.
    MAIL_LOGO_URL: str = "https://medicosporvenezuela.org/brand/logo-white-email.png"

    # --- Kit (correo masivo de las encuestas de marketing) ---
    # Clave de la API v4 de Kit (Kit → Settings → Developer). Solo LEE las métricas de los
    # envíos —enviados, aperturas, clics, bajas— para cruzarlas con las respuestas en el panel.
    # Vacía, el panel muestra solo lo que sabe la plataforma, sin error. Nunca se loguea ni sale
    # del backend: da acceso a la cuenta de Kit, incluida la lista de suscriptores.
    KIT_API_KEY: str = ""
    KIT_API_BASE_URL: str = "https://api.kit.com/v4"
    # Cuánto se reutilizan las métricas de Kit antes de volver a pedirlas. Cambian despacio (las
    # aperturas llegan a lo largo de horas) y cada carga del panel son varias peticiones a Kit.
    KIT_STATS_CACHE_SECONDS: int = 300
    # Solo cuentan los envíos de Kit desde este día (hora de Venezuela): las encuestas salieron el
    # 12 de septiembre de 2026, y lo anterior de la cuenta no es de esta campaña.
    MARKETING_CAMPAIGNS_SINCE: date = date(2026, 9, 1)

    # --- CORS ---
    BACKEND_CORS_ORIGINS: str = "*"

    # --- Dirección cifrada E2E (v1:base64 sealed box) ---
    # Emails (coma-separados) que pueden ver la DIRECCIÓN cifrada de cualquier paciente, además del
    # médico tratante. La dirección va cifrada E2E: esto solo controla a quién se le entrega la
    # ciphertext. Default: la responsable de protección de datos.
    ADDRESS_VIEWER_EMAILS: str = "orianaramirez@gmail.com"

    @property
    def address_viewer_emails(self) -> frozenset[str]:
        return frozenset(
            e.strip().lower() for e in self.ADDRESS_VIEWER_EMAILS.split(",") if e.strip()
        )

    # --- Anti-abuso (rate limiting) ---
    # Storage en memoria por proceso; para varias instancias, usar Redis.
    RATE_LIMIT_ENABLED: bool = True
    DOCTOR_REGISTER_RATE_LIMIT: str = "5/minute"
    # Chequeo previo del registro de médico (¿correo/cédula ya registrados?). Lo dispara el
    # formulario al salir del campo de correo y otra vez al enviar, así que necesita más holgura
    # que el alta; el tope está para frenar a quien quiera recorrer una lista de correos.
    REGISTRATION_CHECK_RATE_LIMIT: str = "20/minute"
    # Escrituras públicas (alta de paciente y de consulta): sin límite, cualquiera puede
    # inundar la cola con casos falsos que los médicos ven en el panel.
    PUBLIC_WRITE_RATE_LIMIT: str = "10/minute"
    # Pedir una interconsulta es un AMPLIFICADOR: una petición autenticada dispara hasta
    # MAIL_FANOUT_MAX correos a médicos reales. Sin tope, una cuenta comprometida convierte
    # la plataforma en un emisor de spam contra sus propios usuarios.
    INTERCONSULTATION_REQUEST_RATE_LIMIT: str = "10/minute"
    # Respuestas a las encuestas de marketing (formulario público). Más holgado que
    # PUBLIC_WRITE_RATE_LIMIT a propósito: las respuestas llegan en RÁFAGA justo después de cada
    # correo masivo, y un 429 ahí es un médico que quería participar y se va. El abuso cuesta
    # poco en comparación —no llega a ningún médico ni entra a la cola; como mucho, filas basura
    # en un listado—, así que el tope solo tiene que frenar un script, no a una campaña.
    # Hasta que uvicorn confió en Caddy (docs/proxy-e-ip-real.md) este tope lo compartían TODOS
    # los que respondían a la vez; ahora es por IP, pero una oficina o un NAT móvil siguen
    # compartiendo IP, así que no se ajusta al volumen de una sola persona.
    SURVEY_RESPONSE_RATE_LIMIT: str = "60/minute"

    def _normalize_async_scheme(self, url: str) -> str:
        """Garantiza el driver async (postgresql+asyncpg://)."""
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql+psycopg2://"):
            return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    @property
    def sqlalchemy_database_uri(self) -> str:
        """URL de conexión async para SQLAlchemy (sin parámetros de SSL)."""
        if self.DATABASE_URL:
            # Quita un eventual ?sslmode=... que asyncpg no entiende.
            base = self._normalize_async_scheme(self.DATABASE_URL)
            return base.split("?", 1)[0]
        user = quote_plus(self.POSTGRES_USER)
        password = quote_plus(self.POSTGRES_PASSWORD)
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def ssl_required(self) -> bool:
        return self.POSTGRES_SSLMODE.lower() in _SSL_REQUIRED

    @property
    def connect_args(self) -> dict[str, Any]:
        """Argumentos de conexión para asyncpg."""
        args: dict[str, Any] = {}
        mode = self.POSTGRES_SSLMODE.lower()
        if mode in _SSL_VERIFY:
            # Verifica la CA (necesita la CA en el trust store; p. ej. la de Supabase).
            args["ssl"] = True
        elif mode in _SSL_NO_VERIFY:
            # require: cifra pero NO verifica la CA (asyncpg ssl=True verificaría =
            # verify-full, que rompe con la CA self-signed del pooler de Supabase).
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            args["ssl"] = ctx
        if self.DB_DISABLE_PREPARED_STATEMENTS:
            # Necesario detrás de PgBouncer en modo transaction (Supabase 6543).
            args["statement_cache_size"] = 0
            args["prepared_statement_cache_size"] = 0
        return args

    @property
    def cors_origins(self) -> list[str]:
        # rstrip("/") tolera barras finales: el navegador manda el header Origin SIN barra, así
        # que un "https://x/" en la env rompía el match exacto de CORS en silencio (sin ACAO).
        return [s for o in self.BACKEND_CORS_ORIGINS.split(",") if (s := o.strip().rstrip("/"))]

    @property
    def internal_mail_recipients(self) -> list[str]:
        """Buzones de operación que reciben los avisos de alta. Vacía = no avisar a nadie.

        Se parte a mano (y no con un `list[str]` de pydantic-settings) porque este repo ya
        resuelve así sus listas de entorno — ver `cors_origins`: pydantic-settings espera JSON
        para un `list[str]` en `.env`, y una coma suelta produciría un error de arranque poco
        obvio.
        """
        return [s for r in self.MAIL_INTERNAL_RECIPIENTS.split(",") if (s := r.strip())]

    @property
    def supabase_storage_url(self) -> str:
        """Base de la API de Storage, sin barra final (`.../storage/v1`)."""
        if self.SUPABASE_STORAGE_URL:
            return self.SUPABASE_STORAGE_URL.rstrip("/")
        return f"{self.SUPABASE_URL.rstrip('/')}/storage/v1"

    @property
    def messaging_allowed_mime_types(self) -> frozenset[str]:
        return frozenset(
            m.strip().lower()
            for m in self.MESSAGING_ALLOWED_ATTACHMENT_MIME_TYPES.split(",")
            if m.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
