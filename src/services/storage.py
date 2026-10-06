"""Almacenamiento de los adjuntos clínicos del chat en Supabase Storage (bucket PRIVADO).

Los binarios viven en el bucket `STORAGE_BUCKET_ATTACHMENTS` (`chat-attachments`) bajo
`consultations/{consultation_id}/attachments/{attachment_id}.bin` (CA15.3 y R1): nombre de
objeto por UUID, sin el nombre real del archivo (ese va cifrado en
`message_attachments.file_name`) y sin nada adivinable desde fuera.

Reglas de este módulo:

- **Bucket privado, cero URLs públicas.** No se generan `publicUrl` ni URLs firmadas: la
  descarga sigue pasando por `GET /consultations/{id}/attachments/{id}`, que exige grant
  clínico y audita (`READ_CLINICAL_DATA`). Este módulo solo lee y escribe bytes.
- **Se habla con Storage por su API REST con el `service_role`** (mismo patrón que
  `services/users.py` con la Admin API de Auth): el bucket es privado, así que el `anon key`
  no sirve. La clave nunca se loguea ni sale de aquí.
- **Nada de PII en logs**: se registra el status HTTP y el tipo de excepción, nunca la ruta
  del objeto, el nombre del archivo ni el contenido.

Antes vivía en `/tmp/medico-storage`: legible por cualquier usuario del host, perdido al
reiniciar el contenedor e invisible entre réplicas. Son PDFs e imágenes clínicas.

Provisión (ver README → "Adjuntos del chat"): el bucket se declara en `supabase/config.toml`
para el Supabase local y hay que crearlo **privado** en el proyecto de producción.
"""

import logging
import os

import httpx

from src.core.config import settings
from src.core.errors import UpstreamServiceError

logger = logging.getLogger("mpv.messaging")

# Generoso respecto a users.py (5 s): aquí viajan hasta 10 MB de binario, no un JSON.
_TIMEOUT = 20.0
# Solo para tests: `httpx.MockTransport` en vez de la red (mismo patrón que services/kit.py).
_transport: httpx.AsyncBaseTransport | None = None


def _storage_headers() -> dict[str, str]:
    key = settings.SUPABASE_SERVICE_ROLE_KEY
    return {"Authorization": f"Bearer {key}", "apikey": key}


def _safe_object_path(storage_path: str) -> str:
    """Normaliza la ruta del objeto y corta cualquier intento de path traversal."""
    normalized = os.path.normpath(storage_path).replace("\\", "/").lstrip("/")
    if not normalized or normalized == "." or normalized.startswith(".."):
        raise UpstreamServiceError("Ruta de almacenamiento inválida.")
    return normalized


def _object_url(storage_path: str) -> str:
    bucket = settings.STORAGE_BUCKET_ATTACHMENTS
    return f"{settings.supabase_storage_url}/object/{bucket}/{_safe_object_path(storage_path)}"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT, transport=_transport)


async def save_attachment_file(
    storage_path: str, content: bytes, content_type: str = "application/octet-stream"
) -> str:
    """Sube el binario al bucket privado y devuelve la ruta del objeto.

    `x-upsert` evita que el reintento de una subida interrumpida choque con el objeto a
    medio escribir: la ruta ya es única por `attachment_id`, así que sobreescribir solo
    puede ser el mismo archivo otra vez.
    """
    try:
        async with _client() as client:
            response = await client.post(
                _object_url(storage_path),
                content=content,
                headers={
                    **_storage_headers(),
                    "Content-Type": content_type,
                    "x-upsert": "true",
                },
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error("Supabase Storage upload error status=%s", exc.response.status_code)
        raise UpstreamServiceError("Fallo al guardar el archivo adjunto.") from exc
    except httpx.RequestError as exc:
        logger.error("Supabase Storage upload connection error type=%s", type(exc).__name__)
        raise UpstreamServiceError("Fallo al guardar el archivo adjunto.") from exc
    return storage_path


async def get_attachment_file(storage_path: str) -> bytes | None:
    """Descarga el binario del bucket privado. `None` si el objeto no existe."""
    try:
        async with _client() as client:
            response = await client.get(_object_url(storage_path), headers=_storage_headers())
            if response.status_code in (400, 404):
                # Storage responde 400 "Object not found" en algunas versiones.
                return None
            response.raise_for_status()
            return response.content
    except httpx.HTTPStatusError as exc:
        logger.error("Supabase Storage download error status=%s", exc.response.status_code)
        raise UpstreamServiceError("Fallo al leer el archivo adjunto.") from exc
    except httpx.RequestError as exc:
        logger.error("Supabase Storage download connection error type=%s", type(exc).__name__)
        raise UpstreamServiceError("Fallo al leer el archivo adjunto.") from exc


async def delete_attachment_file(storage_path: str) -> bool:
    """Borra el objeto. Devuelve False si no existía (no es un error)."""
    try:
        async with _client() as client:
            response = await client.delete(_object_url(storage_path), headers=_storage_headers())
            if response.status_code in (400, 404):
                return False
            response.raise_for_status()
            return True
    except httpx.HTTPStatusError as exc:
        logger.error("Supabase Storage delete error status=%s", exc.response.status_code)
        raise UpstreamServiceError("Fallo al eliminar el archivo adjunto.") from exc
    except httpx.RequestError as exc:
        logger.error("Supabase Storage delete connection error type=%s", type(exc).__name__)
        raise UpstreamServiceError("Fallo al eliminar el archivo adjunto.") from exc
