"""Pruebas de los guards de arranque que fallan rápido en producción con configuración
insegura: los secretos con su valor por defecto (`SUPABASE_JWT_SECRET`,
`SUPABASE_SERVICE_ROLE_KEY`, `EMAIL_VERIFICATION_SECRET`, `CONSULTATION_TOKEN_SECRET`,
`CLINICAL_DATA_ENCRYPTION_KEY`), el flag `EMAIL_VERIFICATION_DEBUG_CODE` activo, y
`BACKEND_CORS_ORIGINS` en '*', que con allow_credentials=True aceptaría credenciales desde
cualquier origen.

⚠️ **Estos tests no deben depender del `.env` de quien los ejecuta.** Los guards se evalúan EN
ORDEN y abortan en el primero que falla, así que cualquier variable que el `.env` local deje en
un valor inseguro se adelanta al mensaje que el test espera y lo rompe sin que el código tenga
nada que ver. Pasó de verdad: un `EMAIL_VERIFICATION_DEBUG_CODE=true` puesto en local para poder
grabar una demo tumbó siete tests de este archivo, que fallaban con el mensaje del `debug_code`
en vez del de CORS o del secreto que estaban probando. Por eso `_prod_secrets` fija **todas** las
entradas de todos los guards, y cada test rompe después solo la que afirma.
"""

import pytest

import src.main as main_module


def _prod_secrets(monkeypatch) -> None:
    """Deja una configuración de producción COMPLETAMENTE VÁLIDA: con esto, `lifespan` no debe
    lanzar nada. Cada test rompe a propósito la única pieza que quiere ejercitar.

    Tiene que cubrir todos los guards, no solo los anteriores al que se prueba: así el test es
    independiente del orden de los guards, del `.env` de la máquina y de los demás tests. Si se
    añade un guard nuevo a `lifespan`, su entrada se fija aquí en el mismo cambio.
    """
    # 1. Secreto del JWT de Supabase.
    monkeypatch.setattr(main_module.settings, "SUPABASE_JWT_SECRET", "un-jwt-secret-de-produccion")
    # 2. service_role key.
    monkeypatch.setattr(
        main_module.settings, "SUPABASE_SERVICE_ROLE_KEY", "un-service-role-key-real-de-produccion"
    )
    # 3. Flag dev/e2e que expondría el código OTP en la respuesta del endpoint de envío. Se fija
    # en False a propósito: es el que suele estar en `true` en un `.env` de desarrollo.
    monkeypatch.setattr(main_module.settings, "EMAIL_VERIFICATION_DEBUG_CODE", False)
    # 4. Secreto con el que se firman los tokens de verificación de correo.
    monkeypatch.setattr(
        main_module.settings, "EMAIL_VERIFICATION_SECRET", "un-secreto-de-verificacion-real"
    )
    # 5. Secreto con el que se firman los accesos a las salas de los pacientes anónimos.
    monkeypatch.setattr(
        main_module.settings, "CONSULTATION_TOKEN_SECRET", "un-secreto-real-de-produccion"
    )
    # 6. Clave de cifrado clínico (la de desarrollo está en el repo).
    monkeypatch.setattr(
        main_module.settings,
        "CLINICAL_DATA_ENCRYPTION_KEY",
        "cHJvZHVjY2lvbi1jbGF2ZS1jbGluaWNhLTMyYnl0ZXM=",
    )
    # 7. Orígenes CORS explícitos (el default es '*', y en el `.env` de dev suele estarlo).
    monkeypatch.setattr(
        main_module.settings, "BACKEND_CORS_ORIGINS", "https://medicosporvenezuela.org"
    )


async def test_lifespan_falla_en_prod_si_el_secreto_jwt_es_default(monkeypatch) -> None:
    """Con el secreto por defecto cualquiera podría firmarse un JWT de admin."""
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    _prod_secrets(monkeypatch)
    monkeypatch.setattr(
        main_module.settings, "SUPABASE_JWT_SECRET", main_module._INSECURE_JWT_DEFAULT
    )

    with pytest.raises(RuntimeError, match="SUPABASE_JWT_SECRET"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_falla_en_prod_si_service_role_key_default(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    _prod_secrets(monkeypatch)
    monkeypatch.setattr(
        main_module.settings,
        "SUPABASE_SERVICE_ROLE_KEY",
        main_module._INSECURE_SERVICE_ROLE_DEFAULT,
    )

    with pytest.raises(RuntimeError, match="SUPABASE_SERVICE_ROLE_KEY"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_ok_en_prod_con_service_role_key_configurado(monkeypatch) -> None:
    """Con todo configurado, arranca. Es el control de que los demás tests fallan por lo que
    rompen y no por el entorno: si este pasa, `_prod_secrets` deja una config válida."""
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    _prod_secrets(monkeypatch)

    async with main_module.lifespan(main_module.app):
        pass


async def test_lifespan_falla_en_prod_si_cors_es_wildcard(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    _prod_secrets(monkeypatch)
    monkeypatch.setattr(main_module.settings, "BACKEND_CORS_ORIGINS", "*")

    with pytest.raises(RuntimeError, match="BACKEND_CORS_ORIGINS"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_falla_en_prod_si_el_wildcard_va_entre_otros_origenes(monkeypatch) -> None:
    """El '*' cuela igual aunque venga acompañado: la lista se valida entera, no solo si es '*'."""
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    _prod_secrets(monkeypatch)
    monkeypatch.setattr(
        main_module.settings, "BACKEND_CORS_ORIGINS", "https://medicosporvenezuela.org,*"
    )

    with pytest.raises(RuntimeError, match="BACKEND_CORS_ORIGINS"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_falla_en_prod_si_el_secreto_del_token_de_sala_es_default(
    monkeypatch,
) -> None:
    """Con el secreto por defecto cualquiera podría firmarse su propio acceso a cualquier sala."""
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    _prod_secrets(monkeypatch)
    monkeypatch.setattr(
        main_module.settings,
        "CONSULTATION_TOKEN_SECRET",
        main_module._INSECURE_CONSULTATION_TOKEN_DEFAULT,
    )

    with pytest.raises(RuntimeError, match="CONSULTATION_TOKEN_SECRET"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_ok_en_dev_aunque_cors_sea_wildcard(monkeypatch) -> None:
    """El guard es solo de produccion: en dev el '*' sigue siendo comodo y no rompe nada."""
    monkeypatch.setattr(main_module, "_IS_PROD", False)
    monkeypatch.setattr(main_module.settings, "BACKEND_CORS_ORIGINS", "*")

    async with main_module.lifespan(main_module.app):
        pass


async def test_lifespan_falla_en_prod_si_clave_clinica_default(monkeypatch) -> None:
    """La clave clínica por defecto está en el repo: en prod cifraría con una clave pública."""
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    _prod_secrets(monkeypatch)
    monkeypatch.setattr(
        main_module.settings,
        "CLINICAL_DATA_ENCRYPTION_KEY",
        main_module._INSECURE_CLINICAL_KEY_DEFAULT,
    )

    with pytest.raises(RuntimeError, match="CLINICAL_DATA_ENCRYPTION_KEY"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_clave_clinica_default_con_espacio_tambien_falla(monkeypatch) -> None:
    """Un salto de línea al final de la env no debe colar la clave pública de desarrollo."""
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    _prod_secrets(monkeypatch)
    monkeypatch.setattr(
        main_module.settings,
        "CLINICAL_DATA_ENCRYPTION_KEY",
        main_module._INSECURE_CLINICAL_KEY_DEFAULT + "\n",
    )

    with pytest.raises(RuntimeError, match="CLINICAL_DATA_ENCRYPTION_KEY"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_falla_en_prod_si_el_secreto_de_verificacion_es_default(
    monkeypatch,
) -> None:
    """Con el secreto por defecto cualquiera podría firmar tokens de verificación de correo."""
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    _prod_secrets(monkeypatch)
    monkeypatch.setattr(
        main_module.settings,
        "EMAIL_VERIFICATION_SECRET",
        main_module._INSECURE_EMAIL_VERIFICATION_DEFAULT,
    )

    with pytest.raises(RuntimeError, match="EMAIL_VERIFICATION_SECRET"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_falla_en_prod_si_el_debug_code_esta_activo(monkeypatch) -> None:
    """El flag dev/e2e expondría el código OTP en la respuesta del endpoint de envío.

    El `True` se pone AQUÍ y no se hereda del `.env`: así este test sigue probando el guard
    aunque en local el flag esté apagado (y los demás siguen pasando aunque esté encendido)."""
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    _prod_secrets(monkeypatch)
    monkeypatch.setattr(main_module.settings, "EMAIL_VERIFICATION_DEBUG_CODE", True)

    with pytest.raises(RuntimeError, match="EMAIL_VERIFICATION_DEBUG_CODE"):
        async with main_module.lifespan(main_module.app):
            pass
