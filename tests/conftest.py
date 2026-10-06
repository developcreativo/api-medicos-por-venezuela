"""Fixtures de pruebas asíncronas.

Aislamiento por SAVEPOINTS: cada test corre sobre una conexión con una transacción
externa abierta y una sesión en modo `create_savepoint`; al final se hace rollback.

Autenticación: las pruebas montan un JWT firmado con el mismo secreto de desarrollo
(`settings.SUPABASE_JWT_SECRET`). El `client` por defecto va autenticado como un
**admin** (cubre staff+admin), así los tests de negocio se centran en la lógica.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.ratelimit import limiter
from src.db.session import engine, get_db
from src.main import app
from src.models.profile import Profile
from tests._helpers import auth_headers

# El rate limiting se desactiva en las pruebas: varias hacen múltiples requests al
# mismo endpoint y dispararían el 429.
limiter.enabled = False
# Gate de verificación de correo: apagado en tests (los tests que lo prueban lo
# reactivan con monkeypatch).
settings.EMAIL_VERIFICATION_REQUIRED = False


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with engine.connect() as conn:
        await conn.begin()
        session = AsyncSession(
            bind=conn,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )
        try:
            yield session
        finally:
            await session.close()
            await conn.rollback()


@pytest_asyncio.fixture
async def admin_identity(db_session: AsyncSession) -> Profile:
    """Inserta un perfil admin (visible para la sesión del test) y lo devuelve."""
    profile = Profile(
        id=uuid.uuid4(),
        full_name="Test Admin",
        role="admin",
        active=True,
        verified=True,
        role_chosen=True,
    )
    db_session.add(profile)
    await db_session.flush()
    return profile


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession, admin_identity: Profile
) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=auth_headers(admin_identity.id),
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def anon_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Cliente SIN sesión, para los endpoints que sirven al paciente anónimo.

    `client` va autenticado como admin, así que no sirve para probar que algo se rechaza sin
    credenciales: pasaría por la puerta de staff y el test daría un falso verde."""

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def live_client() -> AsyncGenerator[AsyncClient, None]:
    """Cliente sin override: cada request usa su propia sesión/conexión real."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
