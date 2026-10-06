"""Manejadores globales de excepciones.

Traducen las excepciones de dominio (capa de servicios) y las nativas de
SQLAlchemy en respuestas HTTP semánticas, sin filtrar detalles internos al
cliente y manteniendo los routers delgados (sin try/except repetidos).
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from src.core.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    TooManyRequestsError,
    UnprocessableError,
    UpstreamServiceError,
)

logger = logging.getLogger("mpv.api")

# SQLSTATE de "lock_not_available" (fallo de with_for_update(nowait=True)).
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


def is_lock_not_available(exc: Exception) -> bool:
    """Detecta el error de fila bloqueada, venga de psycopg2 (OperationalError)
    o de asyncpg (DBAPIError con SQLSTATE 55P03)."""
    for candidate in (exc, getattr(exc, "orig", None)):
        sqlstate = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if sqlstate == LOCK_NOT_AVAILABLE_SQLSTATE:
            return True
    return False


def _safe(exc: Exception) -> str:
    """Resumen logueable de un error de base de datos, SIN PII.

    `str()` de una excepción de SQLAlchemy incluye "[SQL: ...] [parameters: (...)]", es decir
    nombre, cédula y teléfono del paciente en los logs. Aquí solo el tipo y el SQLSTATE
    (mismo criterio que el handler de UpstreamServiceError)."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return f"{type(exc).__name__}(sqlstate={sqlstate})" if sqlstate else type(exc).__name__


def _json(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def register_exception_handlers(app: FastAPI) -> None:
    # --- Excepciones de dominio (capa de servicios) ---
    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return _json(status.HTTP_404_NOT_FOUND, str(exc) or "Recurso no encontrado.")

    @app.exception_handler(ConflictError)
    async def _conflict(request: Request, exc: ConflictError) -> JSONResponse:
        return _json(status.HTTP_409_CONFLICT, str(exc) or "Conflicto de estado.")

    @app.exception_handler(BadRequestError)
    async def _bad_request(request: Request, exc: BadRequestError) -> JSONResponse:
        return _json(status.HTTP_400_BAD_REQUEST, str(exc) or "Petición inválida.")

    @app.exception_handler(UnprocessableError)
    async def _unprocessable(request: Request, exc: UnprocessableError) -> JSONResponse:
        return _json(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc) or "Datos inválidos.")

    @app.exception_handler(ForbiddenError)
    async def _forbidden(request: Request, exc: ForbiddenError) -> JSONResponse:
        return _json(status.HTTP_403_FORBIDDEN, str(exc) or "Acción no permitida.")

    @app.exception_handler(TooManyRequestsError)
    async def _too_many_requests(request: Request, exc: TooManyRequestsError) -> JSONResponse:
        return _json(
            status.HTTP_429_TOO_MANY_REQUESTS,
            str(exc) or "Demasiadas peticiones. Inténtalo de nuevo más tarde.",
        )

    @app.exception_handler(ServiceUnavailableError)
    async def _service_unavailable(request: Request, exc: ServiceUnavailableError) -> JSONResponse:
        logger.error("ServiceUnavailableError en %s: %s", request.url.path, type(exc).__name__)
        return _json(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Servicio no disponible temporalmente. Inténtalo de nuevo más tarde.",
        )

    @app.exception_handler(UpstreamServiceError)
    async def _upstream(request: Request, exc: UpstreamServiceError) -> JSONResponse:
        # Nunca logueamos el body de la respuesta upstream ni el service-role key: solo
        # el tipo de excepción (mirror de sacs.py). El mensaje al cliente es genérico.
        logger.error("UpstreamServiceError en %s: %s", request.url.path, type(exc).__name__)
        return _json(
            status.HTTP_502_BAD_GATEWAY,
            "Servicio externo no disponible. Inténtalo de nuevo más tarde.",
        )

    # --- Excepciones nativas de la base de datos ---
    @app.exception_handler(OperationalError)
    async def _operational(request: Request, exc: OperationalError) -> JSONResponse:
        # Típicamente: fila bloqueada por otra transacción (with_for_update nowait).
        logger.warning("OperationalError en %s: %s", request.url.path, _safe(exc))
        return _json(
            status.HTTP_409_CONFLICT,
            "Conflicto de concurrencia: el recurso está siendo modificado por otra "
            "operación. Inténtalo de nuevo.",
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError) -> JSONResponse:
        # Típicamente: violación de UNIQUE o FOREIGN KEY.
        logger.warning("IntegrityError en %s: %s", request.url.path, _safe(exc))
        return _json(
            status.HTTP_409_CONFLICT,
            "Conflicto de integridad de datos (duplicado o referencia inexistente).",
        )

    @app.exception_handler(DBAPIError)
    async def _dbapi(request: Request, exc: DBAPIError) -> JSONResponse:
        # asyncpg mapea LockNotAvailableError (55P03) a DBAPIError, no a OperationalError.
        if is_lock_not_available(exc):
            logger.warning("Lock no disponible en %s: %s", request.url.path, _safe(exc))
            return _json(
                status.HTTP_409_CONFLICT,
                "Conflicto de concurrencia: el recurso está bloqueado por otra "
                "operación. Inténtalo de nuevo.",
            )
        logger.error("DBAPIError en %s: %s", request.url.path, _safe(exc))
        return _json(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Error interno de base de datos.",
        )
