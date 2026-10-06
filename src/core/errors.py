"""Excepciones de dominio que lanza la capa de servicios.

Son agnósticas de HTTP: los servicios no conocen FastAPI. Los manejadores
globales (ver core/exceptions.py) las traducen a respuestas HTTP.
"""


class NotFoundError(Exception):
    """El recurso solicitado no existe. -> HTTP 404."""


class ConflictError(Exception):
    """Conflicto de estado o de concurrencia. -> HTTP 409."""


class BadRequestError(ValueError):
    """Petición inválida por reglas de negocio. -> HTTP 400."""


class UnprocessableError(ValueError):
    """Datos sintácticamente válidos pero semánticamente inválidos. -> HTTP 422."""


class ForbiddenError(Exception):
    """Acción prohibida por una regla de autorización de negocio (no de permiso base).
    -> HTTP 403. Distinta de un 403 de `require_permission`: esta se lanza desde la capa
    de servicios cuando la regla depende del estado del recurso o del actor (p. ej. solo
    un super_admin puede otorgar super_admin), no de la sola posesión de un permiso.
    """


class UpstreamServiceError(Exception):
    """Un servicio externo (p. ej. Supabase Admin API) falló o es inalcanzable.
    -> HTTP 502. Nunca debe llevar el cuerpo de la respuesta upstream ni secretos."""


class TooManyRequestsError(Exception):
    """Demasiadas peticiones (rate limit o cooldown de negocio). -> HTTP 429.
    El mensaje está pensado para el usuario final, sin filtrar detalles internos."""


class ServiceUnavailableError(Exception):
    """El servicio no está disponible temporalmente (p. ej. Mailtrap sin token o caído).
    -> HTTP 503. El mensaje es genérico para el cliente."""
