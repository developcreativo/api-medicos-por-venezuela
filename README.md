# API Médicos por Venezuela

API REST construida con **FastAPI + SQLAlchemy + Pydantic**.

- **Desarrollo local:** [Supabase local](#supabase-local-desarrollo) (CLI de Supabase,
  Docker por debajo) — el **mismo** Postgres/Auth/Realtime que usa el frontend, no un
  Postgres propio. Local y producción usan el mismo esquema y el mismo Supabase Auth;
  no hay una versión "de mentira" del entorno.
- **Producción:** se conecta a **Supabase** (Postgres) vía variables de entorno.

Los modelos reflejan el esquema **real y actual** de Supabase (12 tablas: `profiles`,
`patients`, `doctors`, `consultations`, `consultation_events`, `prescriptions`,
`referrals`, `rest_notes`, `treatment_plans`, `follow_ups`, `messages`, `admin_users`).

## Stack

- **FastAPI (async)** — framework web + OpenAPI automático; todas las rutas son `async def`.
- **SQLAlchemy 2.0 async** — ORM con driver **asyncpg** (`AsyncSession`).
- **Pydantic v2** + **pydantic-settings** — validación (`EmailStr`, `Field`) y configuración.
- **PostgreSQL 17** — Supabase local (CLI) en dev / Supabase en prod (mismo motor).
- **uv** (Astral) — gestor de paquetes y entornos.
- **pytest + pytest-asyncio** — suite async con cobertura (objetivo ≥95%).
- **Ruff** — lint + formato.
- **Docker / Docker Compose** — solo el servicio `api` (Postgres/Auth/Realtime los da
  Supabase local, que también corre en Docker por debajo del CLI).

## Arquitectura (capa de servicios, 3-tier)

```
artisan                # CLI del proyecto (estilo Laravel): python artisan migrate / make:migration
src/
├── main.py            # App FastAPI, CORS, registro de manejadores globales
├── core/
│   ├── config.py      # Settings por entorno (URL async, SSL, pooler)
│   ├── errors.py      # Excepciones de dominio (NotFound/Conflict/BadRequest/...)
│   └── exceptions.py  # Manejadores globales -> respuestas HTTP semánticas
├── db/                # Base declarativa + engine/sesión async
├── models/            # Capa de BD: modelos ORM (12 tablas reales)
├── schemas/           # Capa de validación: Pydantic (Create/Update/Response)
├── services/          # Capa de negocio: lógica + queries + bloqueos (async)
└── routers/           # Capa HTTP: routers DELGADOS que delegan en services
db/migrations/         # Migraciones de esquema (.sql), aplicadas por scripts/migrate.py
                       # 000_core_schema.sql = espejo de supabase_schema.sql del frontend
                       # (profiles/patients/consultations/RLS/RPCs); el resto es de este repo.
supabase/              # Config del CLI de Supabase LOCAL (`npx supabase start`). Sin
                       # migraciones propias: el schema completo lo aplica scripts/migrate.py.
backups/               # Backups de Supabase (.dump/.sql) — IGNORADO por git (PII)
scripts/               # backup_supabase.sh / load_local.sh / migrate.py (CLI de migraciones)
tests/                 # Suite async (CRUD aislado por savepoints + concurrencia)
```

- **Routers** (capa HTTP): reciben la petición, inyectan `AsyncSession` con `Depends`
  y delegan en el servicio. Sin lógica ni queries.
- **Services** (capa de negocio): funciones async puras; reciben `session` y datos planos,
  ejecutan SQLAlchemy y lanzan **excepciones de dominio** (no `HTTPException`).
- Los **manejadores globales** (`core/exceptions.py`) traducen las excepciones de dominio
  y las nativas de SQLAlchemy (`OperationalError`, `IntegrityError`, lock `55P03`) a HTTP.

## Supabase local (desarrollo)

Local y producción usan **el mismo** Supabase: mismo esquema, mismo Supabase Auth, mismo
Realtime. No hay una versión "de mentira" del backend para local — el único cambio entre
entornos es a qué **proyecto** de Supabase apuntás (local vs. el de producción).

### 1. Prerequisitos (una sola vez)

- **Docker Desktop** corriendo (Supabase local corre en Docker por debajo del CLI).
- **Node.js** (para invocar el CLI de Supabase vía `npx`; no hace falta instalarlo global).

### 2. Instalar el CLI de Supabase (pinneado en este repo)

Este repo es Python, pero el CLI de Supabase se distribuye vía npm — se pinnea la versión
en un `package.json` mínimo (dev-only) para que todos los devs usen la misma:

```bash
npm install        # instala el CLI de Supabase pinneado en package.json (solo para esto)
```

A partir de acá, todo comando del CLI se invoca como `npx supabase <comando>`.

### 3. Levantar Supabase local

```bash
npx supabase start
```

La **primera vez** descarga varias imágenes Docker (unos minutos); las siguientes tarda
~30s. Al terminar imprime las URLs/keys locales (`ANON_KEY`, `API_URL`, `DB_URL`, `JWT_SECRET`,
`STUDIO_URL`...) — son **valores fijos de desarrollo local**, no secretos reales, y ya están
precargados en `.env`/`.env.example` de este repo.

Queda corriendo (todo en Docker, nombres `supabase_*_api-medicos-por-venezuela`):

| Servicio | URL |
| -------- | --- |
| API (Auth/REST/Realtime, vía Kong) | http://localhost:54321 |
| Postgres | `localhost:54322` (user/pass/db: `postgres`) |
| Studio (UI para explorar la BD) | http://localhost:54323 |
| Inbucket (atrapa los emails de Auth) | http://localhost:54324 |

> Deshabilitados a propósito (ver `supabase/config.toml`): Storage, Edge Functions y
> Analytics — este proyecto solo usa **Auth** y **Realtime** de Supabase; menos
> contenedores = arranque más liviano. Se pueden reactivar ahí si hicieran falta.

Para apagarlo: `npx supabase stop`. Para un reinicio total (borra los datos locales):
`npx supabase stop --no-backup && npx supabase start`.

### 4. Aplicar el schema completo (una sola vez por BD nueva)

El schema **no** vive en `supabase/migrations/` — sigue viviendo en `db/migrations/` de este
repo (el runner de siempre, `artisan`/`scripts/migrate.py`), incluyendo el schema "core"
(profiles/patients/consultations/RLS/RPCs) copiado 1:1 del frontend como
`db/migrations/000_core_schema.sql` (ordena primero). Supabase local ya trae su propio
esquema `auth` real (Auth/GoTrue), así que no hace falta ningún stub.

```bash
cp .env.example .env      # ya apunta a Supabase local (puerto 54322 + JWKS de Auth)
python artisan migrate    # aplica las 12+ migraciones (schema core + catálogos + RBAC + ...)
```

### 5. Levantar la API

```bash
# Nativo (más simple, sin Docker para la API):
uv run uvicorn src.main:app --reload

# O en Docker (requiere Supabase local ya corriendo -> se conecta por host.docker.internal):
docker compose up --build
```

- API: http://localhost:8000 · Swagger: http://localhost:8000/docs · Health: `/api/v1/health`

### Un comando (para el frontend / sin saber Python)

`dev.sh`/`dev.ps1` hacen los pasos 3–5 en un solo comando (arrancan Supabase local si hace
falta, levantan la API en Docker, y migran):

```bash
# Mac / Linux / Git-Bash:
./dev.sh                 # levanta Supabase local + api y migra   ·   ./dev.sh down para apagar la api

# Windows (PowerShell):
.\dev.ps1                # idem   ·   .\dev.ps1 down para apagar la api
```

**La primera vez** que se corren (base recién creada, sin `schema_migrations` todavía), si
existe un dump en `./backups/*.dump` **y** `.env.supabase` (credenciales de prod), restauran
automáticamente esos datos **reales** antes de migrar — así todos los devs arrancan con el
mismo espejo de producción, no una base vacía. En corridas siguientes **no vuelven a tocar
los datos** (solo aplican migraciones nuevas si las hay).

El deploy a producción (EC2) usa `deploy.sh`, no estos.

### Datos reales de producción en local

> **Por qué:** construir el schema solo desde `db/migrations/` puede quedar desalineado de
> lo que hay *realmente* en prod (columnas agregadas a mano, migraciones que a prod nunca
> llegaron...). Restaurar un dump de prod elimina esa duda: local queda con el schema y los
> datos **exactos** de producción, y encima se aplican las migraciones que prod todavía no
> tiene.

```bash
./scripts/backup_supabase.sh   # (requiere .env.supabase) genera un dump FRESCO de PRODUCCIÓN
./scripts/load_local.sh        # lo restaura en el Postgres de Supabase LOCAL (solo esquema public)
                                # + aplica migraciones pendientes encima (rename, RBAC, etc.)
```

`dev.sh`/`dev.ps1` llaman a `load_local.sh` solos en el primer arranque (ver arriba) si ya
tenés un dump en `./backups/` — no hace falta correr esto a mano salvo que quieras **refrescar**
los datos a lo último de prod.

- **`.env.supabase`** (credenciales de prod, no se versiona) hay que **distribuirlo al equipo
  por un canal seguro** (gestor de secretos, no git/Slack) — es lo mismo que ya requería
  `backup_supabase.sh` antes de este cambio.
- El dump (`backups/*.dump`) tiene **PII real** y tampoco se versiona; para que "todos los
  devs empiecen mañana" con el mismo dump, alguien con acceso a prod lo genera una vez y lo
  comparte por un canal seguro (o cada dev genera el suyo con sus propias credenciales).
- Solo se restaura `--schema=public` (no toca `auth`/`storage`/`realtime` de Supabase local).
  Las FKs de `public` hacia `auth.users` (p. ej. `profiles.id`) no se recrean en la restauración
  (`auth.users` está vacía en local) — es esperado, no un error a corregir.

### JWT: por qué hay dos esquemas (HS256 y JWKS)

El CLI de Supabase local firma los JWT de Auth con **claves asimétricas (ES256, rotables,
"JWT signing keys")**, expuestas en `/auth/v1/.well-known/jwks.json` — no con el secreto
HS256 compartido clásico. La mayoría de proyectos de prod hoy siguen en HS256, así que el
backend soporta **ambos**: mira el `alg` del JWT y valida por JWKS (`SUPABASE_JWKS_URL`) si
es asimétrico, o por secreto compartido (`SUPABASE_JWT_SECRET`) si es HS256 — sin que el
código de negocio note la diferencia. En prod normalmente `SUPABASE_JWKS_URL` queda vacío.

### Docker: cómo encaja todo (local vs. producción)

El mismo `Dockerfile` produce la **misma imagen** de la API en los dos entornos — lo único
que cambia es a qué Postgres/Auth se conecta esa imagen y cómo llega ahí. No hay dos
versiones del código, solo configuración distinta por entorno.

**Local:** son **dos Docker Compose separados, sin relación entre sí**:

1. `docker-compose.yml` (este repo) — hoy tiene **solo el servicio `api`**. Ya no levanta
   Postgres propio (se sacó el viejo servicio `db`).
2. El stack que arma `npx supabase start` — es *su propio* Compose interno (Postgres,
   Auth/GoTrue, Realtime, Kong, Studio, Inbucket...), gestionado por el CLI, no por vos
   directo.

```
docker-compose.yml (repo)                    npx supabase start (stack propio)
┌──────────────────────────┐                 ┌──────────────────────────────────┐
│ mpv-api (FastAPI)        │─ host.docker. ─►│ Auth · Kong · Realtime · Studio   │
│ POSTGRES_HOST=           │   internal      │ Postgres :54322                  │
│   host.docker.internal   │                 └──────────────────────────────────┘
└──────────────────────────┘
```

- **`host.docker.internal`**: `mpv-api` y los contenedores de Supabase **no comparten red**
  de Compose (son proyectos distintos). Supabase publica su Postgres en el puerto `54322`
  **del host**; `host.docker.internal` es el nombre que Docker Desktop resuelve a "la
  máquina host" para que el contenedor llegue ahí. Funciona out-of-the-box en Windows/Mac
  (en Linux nativo sin Docker Desktop hace falta `--add-host=host.docker.internal:host-gateway`).
- **Sin ese truco:** corriendo la API **nativa** (`uv run uvicorn src.main:app --reload`,
  sin Docker) `localhost:54322` funciona directo — más simple para iterar/debuggear.
- **Orden de arranque** (lo que hacen `dev.sh`/`dev.ps1`): primero Supabase local tiene que
  estar arriba, después `docker compose up` para `api`.
- **¿Dónde lo veo en Docker Desktop?** Docker Desktop agrupa contenedores por **proyecto de
  Compose** (el nombre de la carpeta, si no se especifica uno). Vas a ver **dos grupos**: uno
  con ~8 contenedores de Supabase, y otro aparte llamado `api-medicos-por-venezuela` con
  **un solo contenedor**, `mpv-api`. Si no lo ves, buscá ese segundo grupo (no está mezclado
  con el de Supabase) o filtrá por "mpv-api".

**Producción (EC2):** `docker-compose.prod.yml` — también **solo `api`**, esto **nunca
cambió** (prod jamás tuvo un servicio `db` propio). Se conecta al pooler de Supabase
**por internet** (`DATABASE_URL`/`POSTGRES_*` en `.env.production`, `sslmode=require`). No
hay "stack de Supabase" que levantar ahí: en prod, Supabase es un servicio en la nube al que
te conectás, no algo que corras vos — por eso tampoco hace falta `host.docker.internal`.

| | Local | Producción |
| --- | --- | --- |
| Qué corre en Docker | `api` (repo) + stack completo de Supabase (CLI) | Solo `api` |
| Dónde vive Postgres | Contenedor de Supabase local (`:54322`, en el host) | Supabase Cloud (remoto) |
| Cómo llega `api` a Postgres | `host.docker.internal:54322` (o nativo, sin Docker) | Pooler por internet (`DATABASE_URL`) |
| JWT | ES256 vía JWKS (`SUPABASE_JWKS_URL` seteado) | HS256 con secreto compartido (`SUPABASE_JWKS_URL` vacío) |
| Config | `docker-compose.yml` (valores fijos de dev) | `docker-compose.prod.yml` + `.env.production` (secretos reales) |
| Quién llega al `:8000` | Cualquiera (`0.0.0.0`) | Solo el host (`127.0.0.1`): se entra por Caddy |

**Proxy e IP real (prod):** Caddy corre en el host del EC2 y hace `reverse_proxy` a
`127.0.0.1:8000`. uvicorn acepta `X-Forwarded-For` solo del gateway de la red docker de la API
(`FORWARDED_ALLOW_IPS`), así que el `audit_log` y el rate limit ven la IP del cliente y nadie puede
falsificarla. Detalle, verificación y el paso manual previo al primer deploy:
[docs/proxy-e-ip-real.md](docs/proxy-e-ip-real.md).

## Migraciones de esquema

Los cambios de esquema viven como archivos `.sql` en **`db/migrations/`** y se gestionan con el CLI
**`scripts/migrate.py`** (Python, **multiplataforma** Windows/macOS/Linux — usa `asyncpg` y conecta
por TCP, sin depender de bash ni de `docker exec`). Lleva registro de lo aplicado en la tabla
`schema_migrations` (`filename`, `applied_at`) y aplica **solo lo que falta**, en orden, cada
migración en una transacción — da igual cuántas ramas metan migraciones o cuántas veces lo ejecutes.

Se maneja con el CLI `artisan` de la raíz (estilo Laravel). Se auto-ejecuta con el python del
`.venv`, así que no necesitas activar el entorno:

```bash
python artisan migrate           # aplica las pendientes
python artisan migrate:status    # qué está aplicado / pendiente
python artisan "make:migration" "add phone to doctors"   # crea la migración
# Unix/macOS:  ./artisan migrate     ·     con uv:  uv run python artisan migrate
```

La conexión sale de la misma config que la app (`DATABASE_URL` o las piezas `POSTGRES_*` del entorno
/ `.env`). Para producción, exporta `DATABASE_URL` de Supabase antes de correr `migrate`.
(`artisan` es un frente delgado sobre `scripts/migrate.py`, que puedes invocar directo si prefieres.)

### Crear una migración

```bash
python artisan "make:migration" "add phone to doctors"
# -> Crea db/migrations/20260702_115540_add_phone_to_doctors.sql y te muestra la ruta a editar
```

Abres el archivo generado y escribes el SQL, **idempotente** (`if not exists`, `on conflict`) y
**transaccional** (nada de `CREATE INDEX CONCURRENTLY`):

```sql
-- db/migrations/20260702_115540_add_phone_to_doctors.sql
alter table public.doctors add column if not exists phone text;
```

Para ver qué está aplicado y qué falta:

```bash
python artisan migrate:status
#   [aplicada]  001_create_specialties.sql
#   [pendiente] 20260702_115540_add_phone_to_doctors.sql
#   Total: 2 aplicadas, 1 pendientes.
```

Commit + PR normal. **No** ejecutas nada contra ninguna base al escribirla: solo versionas el `.sql`.

### Aplicarlas en tu Postgres local (tras cada `git pull` o `docker compose up`)

```bash
python artisan migrate      # aplica solo las que te falten
```

Si ya las tenías, imprime `aplicadas: 0`. En una base recién levantada (`docker compose up`) el init
restaura el **backup** con el esquema vigente de Supabase; `migrate` aplica el delta pendiente. El
contenedor de Postgres no aplica migraciones por sí solo (no tiene Python).

### Aplicarlas en un entorno compartido / producción (Supabase)

Contra la base de dev o prod (idealmente desde el pipeline al mergear a `dev`):

```bash
DATABASE_URL="postgresql://postgres.<ref>:<pass>@aws-1-...pooler.supabase.com:5432/postgres" \
  python artisan migrate
```

Aplica solo lo pendiente en **esa** base y lo registra en su propia `schema_migrations`.

### Flujo completo

```
dev escribe 003_x.sql ──PR──► merge a dev
                                  │
        ┌─────────────────────────┼──────────────────────────┐
   cada dev hace pull        deploy corre                    (prod cuando toque)
   artisan migrate          DATABASE_URL=... artisan migrate  (mismo, base prod)
   (su docker local)         (base dev Supabase)
```

### Baseline (una sola vez, si una base ya tenía migraciones aplicadas a mano)

Márcalas como aplicadas para que el runner no las reintente (al ser idempotentes tampoco romperían,
pero es higiene):

```sql
INSERT INTO schema_migrations (filename) VALUES
  ('001_create_specialties.sql'),
  ('20260630_create_professional_types.sql')
ON CONFLICT DO NOTHING;
```

> **Modos de `migrate.sh`:** por defecto usa `docker exec` al contenedor `mpv-db` (devs);
> `--local` corre `psql` directo (lo usa el init dentro del contenedor); `--remote` usa `DATABASE_URL`
> (producción). El orden de aplicación es alfabético por nombre de archivo, así que numera/fecha los
> prefijos de forma consistente entre el equipo.

## Ejecutar la API sin Docker (contra el Postgres local) — con `uv`

```bash
uv sync --extra dev                       # crea el entorno e instala dependencias
uv run uvicorn src.main:app --reload      # http://localhost:8000
```

> Necesitas un Postgres local en `localhost:5432`. La forma más simple es levantarlo
> con `docker compose up -d db` (incluye el restore del backup).

## Backups de Supabase

`scripts/backup_supabase.sh` genera un dump (esquema + datos) en `./backups` usando
`pg_dump` desde un contenedor `postgres:17`, conectándose por el **session pooler
(puerto 5432)**. Requiere las credenciales en `.env.supabase` (no se versiona).

```bash
./scripts/backup_supabase.sh
```

> ⚠️ Los backups contienen **PII real** (pacientes, perfiles). El directorio `backups/`
> está en `.gitignore` y **nunca** debe subirse al repositorio.

## Configuración (variables de entorno)

`DATABASE_URL` tiene prioridad; si no, se arma desde las piezas `POSTGRES_*`.

| Variable               | Local (dev)      | Producción (Supabase)                       |
| ---------------------- | ---------------- | ------------------------------------------- |
| `POSTGRES_HOST`        | `localhost`      | `aws-1-us-east-1.pooler.supabase.com`       |
| `POSTGRES_PORT`        | `5432`           | `6543` (transaction pooler)                 |
| `POSTGRES_DB`          | `medicos`        | `postgres`                                  |
| `POSTGRES_USER`        | `postgres`       | `postgres.<project_ref>`                    |
| `POSTGRES_PASSWORD`    | `localdev`       | (secreto de Supabase)                       |
| `POSTGRES_SSLMODE`     | `prefer`         | `require`                                   |
| `SUPABASE_JWT_SECRET`  | (dev por defecto)| **secreto JWT de Supabase** (obligatorio)   |
| `JITSI_DOMAIN`         | `meet.jit.si`    | dominio Jitsi (self-host opcional)          |
| `STALE_CONSULTATION_MINUTES` | `30`       | umbral para liberar consultas estancadas    |
| `BACKEND_CORS_ORIGINS` | `*`              | dominios del frontend                       |
| `MAILTRAP_API_TOKEN`   | (vacío = no envía)| token de Mailtrap (Sending → API Tokens)   |
| `MAIL_INTERNAL_RECIPIENTS` | (vacío)      | **buzones de operación, separados por comas** |
| `CONTACT_EMAIL`        | `info@medicosporvenezuela.org` | dirección pública de contacto |
| `MAIL_LOGO_URL`        | `…/brand/logo-white-email.png` | logotipo del banner de los correos |
| `KIT_API_KEY`          | (vacío = sin métricas de Kit) | **clave API v4 de Kit**, solo lectura de envíos para el embudo de Marketing |
| `KIT_STATS_CACHE_SECONDS` | `300`         | cuánto se reutilizan las métricas de Kit     |
| `MARKETING_CAMPAIGNS_SINCE` | `2026-09-01` | primer día (Caracas) de los envíos de Kit que cuentan |
| `STORAGE_BUCKET_ATTACHMENTS` | `chat-attachments` | **bucket PRIVADO** de los adjuntos del chat (hay que crearlo) |
| `MESSAGING_AFTER_CLOSE_HOURS` | `72`          | horas tras el cierre en las que el hilo sigue admitiendo mensajes |
| `MESSAGING_MAX_BODY_CHARS` | `2000`           | tope del cuerpo de un mensaje (sale en el OpenAPI) |
| `MESSAGING_PATIENT_HOURLY_LIMIT` | `30`       | mensajes del paciente por hilo y hora (además del límite por IP) |

- **Local:** `.env` (copiado de `.env.example`).
- **Producción:** `.env.supabase` (ignorado por git) o el gestor de secretos del hosting.
  ⚠️ `SUPABASE_JWT_SECRET` es **obligatorio** en producción (Supabase → Settings → API → JWT Secret);
  el valor por defecto del código es solo para desarrollo/pruebas.
- `SUPABASE_URL` (URL del proyecto; local: `http://127.0.0.1:54321`, el gateway del CLI) y
  `SUPABASE_SERVICE_ROLE_KEY` (Supabase → Settings → API → `service_role` **secret**) los usan
  **solo dos módulos**: `src/services/users.py` (crear usuarios de Auth vía la Admin API,
  `POST /users`) y `src/services/storage.py` (leer y escribir los adjuntos del chat en el
  bucket privado — ver abajo; el anon key no puede abrir un bucket privado). Igual que
  `SUPABASE_JWT_SECRET`: **obligatorio** en producción, nunca se loguea, el valor por defecto
  del código es solo para desarrollo/pruebas.

### Adjuntos del chat: el bucket privado hay que crearlo

Los adjuntos clínicos del buzón médico ↔ paciente (PDFs e imágenes: informes, exámenes, fotos)
se guardan en el bucket **privado** `chat-attachments` de Supabase Storage, con nombre de objeto
por UUID (`consultations/{consultation_id}/attachments/{attachment_id}.bin`). El nombre real del
archivo va cifrado en `message_attachments.file_name`, nunca en la ruta.

- **Nunca se genera una URL pública ni firmada.** El navegador no habla con Storage: la descarga
  pasa por `GET /api/v1/consultations/{id}/attachments/{attachment_id}`, que valida pertenencia
  (médico tratante o paciente dueño), registra `READ_CLINICAL_DATA` y sirve el binario con
  `X-Content-Type-Options: nosniff`. Un admin que no es el tratante recibe 403.
- **Local:** el bucket lo declara `supabase/config.toml`. Tras un `git pull` que traiga ese
  cambio hace falta `npx supabase stop && npx supabase start` para que el CLI levante el
  contenedor de Storage y lo cree. Los tests **no** lo necesitan: `tests/test_messaging.py`
  inyecta un `httpx.MockTransport` en `src/services/storage.py`.
- **Producción:** hay que crearlo a mano, una vez: Supabase → Storage → *New bucket*, nombre
  `chat-attachments`, con **"Public bucket" DESMARCADO**. No hace falta ninguna policy de RLS
  (la API entra con el service-role key, que las salta). Si falta el bucket, la primera subida
  responde `502` y el log del arranque ya deja dicho qué bucket y qué URL se esperaban.

### Correo: `MAIL_INTERNAL_RECIPIENTS` hay que ponerlo al desplegar

Es la lista de buzones de **operación** que reciben los avisos de alta: paciente nuevo en la
cola pública, y médico registrado (con su veredicto de credencial). Separada por comas.

Viene **vacía por defecto y a propósito**, con el mismo criterio que `MAILTRAP_API_TOKEN`: con
las direcciones reales escritas en el código, cualquier entorno de pruebas que tuviera un token
de Mailtrap le escribiría de verdad a esas personas en el primer registro de prueba.

⚠️ **La contrapartida es que si se olvida en producción, esos avisos no salen** y nadie recibe
un error — es un silencio, no un fallo. Compruébalo tras desplegar registrando un paciente de
prueba. Los correos **al médico** (registro aprobado / rechazado) no dependen de esta variable:
esos salen siempre que haya `MAILTRAP_API_TOKEN`.

En local, define `MAILTRAP_INBOX_ID` para que todo se entregue al inbox de pruebas en vez de
enviarse de verdad.

`CONTACT_EMAIL` es **otra cosa**: la dirección pública a la que se le pide al médico rechazado
que mande su título, su licencia del SACS y su carta de artículo 8. `MAIL_INTERNAL_RECIPIENTS`
es "a quién avisamos" (incluye buzones personales); `CONTACT_EMAIL` es "a dónde escribe la
gente". Por eso sí trae valor por defecto: si quedara vacía, el correo mandaría al médico a
`no-reply@`, o sea a la basura.

### La marca de los correos (`services/mail_layout.py`)

Todo correo sale dentro del mismo documento: banner navy (`#18202b`) con el logotipo blanco
arriba, el contenido sobre blanco, y el pie con la letra pequeña.

La maquetación se aplica en **`mail.send_mail`**, no en cada constructor de cuerpo. Los cuerpos
viven repartidos entre tres módulos y tres de ellos se mandaban solo en texto plano: envolver en
cada uno sería una regla que hay que recordar cada vez, y el correo número doce saldría sin
marca sin que nadie se entere. En el envío no hay camino para mandar uno sin ella (a los de solo
texto se les fabrica el HTML desde su propio texto).

⚠️ **El logotipo es un PNG y vive en el frontend.** Gmail, Outlook y Yahoo descartan un `<img>`
que apunte a un SVG: la cabecera saldría con el icono de imagen rota. Lo genera
`node scripts/build-logo-raster.mjs` en el repo del frontend, desde el mismo `logo-white.svg`, y
se sirve desde el sitio — **tiene que estar desplegado** o el banner llega vacío (navy con el
texto alternativo, no roto, pero vacío). `MAIL_LOGO_URL` apunta a producción incluso en local, a
propósito: con `FRONTEND_URL=localhost` el logotipo llegaría roto a quien reciba un correo
enviado desde una máquina de desarrollo.

Los correos de **Supabase Auth** (confirmar cuenta, recuperar contraseña) no pasan por aquí: sus
plantillas se editan en el panel de Supabase y hay que darles el mismo banner a mano.

### ¿Entró el paciente a la videollamada? (`entered_call_at`)

El médico necesita saber si el paciente llegó, y la presencia por Realtime **no sirve para eso**:
dice si tiene abierta una pestaña del sitio, y al abrir Jitsi esa pestaña pasa a segundo plano —en
móvil el navegador la suspende y se cae el WebSocket—, así que el panel decía "sin conexión"
justo en el momento en que el paciente acababa de entrar.

La señal duradera es `consultations.entered_call_at`, que fija
`POST /consultations/{id}/entered-call` una sola vez. Se expone en el panel
(`PanelConsultationItem`) y el frontend lo pinta como "Entró a la videollamada · hace X".

Los **tres** caminos por los que un paciente entra a su sala lo marcan:

| Desde | Credencial |
| --- | --- |
| `/sala-espera` (tras registrarse) | token de sala de la URL |
| `/mi-caso` (vuelve con su cuenta) | **su sesión** — ver abajo |
| `/entrar-videoconsulta` (enlace del correo) | token fresco emitido con el correo |

`require_consultation_token` acepta desde ahora **la sesión del paciente dueño** además del token
y de una sesión de staff. El token se entrega una sola vez, en la URL de la sala de espera, y
caduca a las 24 h: quien cerró aquella pestaña tiene sesión pero no token. La pertenencia se
comprueba contra `Patient.user_id` (misma regla anti-IDOR de las lecturas), y la sesión no es una
credencial más débil que el token — es más fuerte, porque no viaja por la URL.

El enlace del correo pasa por `/entrar-videoconsulta` (una página del frontend) y **no** por Jitsi
directamente: es lo que registra la entrada. Ese registro lo hace JavaScript a propósito y no un
redirect del backend, porque los escáneres de correo corporativos siguen los enlaces de un mensaje
para analizarlos — un `GET` que marcara la entrada daría "el paciente entró" por culpa de un robot.

## Autenticación y autorización (RBAC granular)

El login sigue en **Supabase Auth**; el frontend manda el JWT como
`Authorization: Bearer <token>`. La API valida firma/exp/audiencia, saca el `sub`
(= id del perfil) y **calcula los permisos efectivos** del usuario.

**Modelo:** RBAC granular **multi-rol**. Un usuario tiene uno o varios **roles**; cada rol agrupa
**permisos** (`recurso.accion`, p. ej. `consultations.close`). El permiso efectivo del usuario es la
**unión** de los permisos de todos sus roles activos. Así un mismo usuario accede a varias
capacidades sin duplicar cuentas.

```
users (profiles) ──< user_roles >── roles ──< role_permissions >── permissions
                      (revoked_at)                                  audit_log (inmutable)
```

**Roles** (`patient | doctor | admin | super_admin`) → permisos:

| Rol | Permisos |
| --- | --- |
| `patient` | ninguno de staff (solo ve **lo suyo** por pertenencia) |
| `doctor` | `consultations.read/write/close`, `queue.read/take`, `patients.read`, `doctors.read` |
| `admin` | todo lo de doctor + `patients.write/delete`, `consultations.delete`, `queue.manage`, `doctors.write/verify`, `profiles.read/manage`, `catalogs.manage`, `roles.assign`, `audit.read`, `users.create` |
| `super_admin` | **todos** los permisos, y el único con `reports.export` (exportación de reportes con PII masiva) y `marketing.read` (respuestas de las encuestas de marketing) |

**Cómo se protege un endpoint** (una línea): `Depends(require_permission("recurso.accion"))` → 403 si
falta el permiso. Se autoriza por **permiso**, no por rol.

- El **actor** de las acciones (médico que toma/cierra, quien asigna un rol) se toma del JWT, **no**
  de ids del cliente (anti-IDOR).
- Un usuario **revocado** (`active=false`) pierde **todos** sus permisos al instante.
- **Coexistencia:** si un usuario aún no tiene filas en `user_roles`, se usa su `profiles.role` como
  fallback (`specialist` legacy → `doctor`). El backfill inicial ya migró todos los perfiles.
- **Catálogos** (`specialties`, `affected_zones`, `professional-types`): **listar es público** (lo usa
  el registro del sitio); el resto del CRUD exige `catalogs.manage` (admin/super_admin).
- **Auditoría:** las acciones sensibles se registran en `audit_log` (append-only, **inmutable** por
  trigger). Se leen con `GET /audit-log` (permiso `audit.read`).
- **Otorgar `super_admin` exige un actor `super_admin`:** `assign_role` (usado tanto por
  `POST /users/{id}/roles` como por el `initial_role` de `POST /users`) rechaza con `403`
  otorgar `super_admin` si el propio actor no lo tiene ya, aunque tenga `roles.assign` por otro
  rol (p. ej. un `admin` plano). `POST /users` además bloquea `super_admin` como `initial_role`
  con `422` para **cualquier** actor (restricción de creación independiente del guard anterior).

- **`reports.export` es exclusivo de `super_admin`** (migración
  `20260903_093414_seed_reports_export_permission.sql`): a diferencia de `stats.read`, que se
  otorgó también a `admin`, los reportes exportan la ficha COMPLETA de médicos y pacientes
  (cédulas, teléfonos, alergias) de miles de personas a un archivo que sale de la plataforma. Es
  la operación con mayor exposición de PII de la API; cada exportación queda en `audit_log` como
  `report.exported` con el filtro aplicado y el número de filas (nunca las filas).

- **`marketing.read` también es exclusivo de `super_admin`** (migración
  `20260912_191154_seed_marketing_read_permission.sql`): da el listado y el Excel de las respuestas
  a las encuestas de marketing, que es la lista de correos de quienes respondieron, y sus totales y
  gráficos (agregados, sin correos). Cada exportación
  queda en `audit_log` como `report.exported` con `report = marketing-<encuesta>`. Si el equipo de
  marketing usa cuentas `admin`, basta una migración que añada ese mapeo.

**Agregar un permiso nuevo:** siémbralo en una migración (`permissions` + `role_permissions`) y
protege el endpoint con `require_permission("...")`. Nunca lo insertes a mano.

## Endpoints (prefijo `/api/v1`)

| Método  | Ruta                                | Descripción                          |
| ------- | ----------------------------------- | ------------------------------------ |
| `GET`   | `/auth/me`                          | Perfil del usuario autenticado       |
| `GET`   | `/queue`                            | Board: consultas en espera (staff)   |
| `POST`  | `/queue/{id}/take`                  | Toma atómica de una consulta (200/409/404) |
| `POST`  | `/queue/attend-next`                | "Atender al siguiente" (selección + toma atómica) |
| `POST`  | `/queue/release-stale`              | Liberar consultas estancadas (admin/CRON) |
| `GET`   | `/specialties`                      | Lista pública de especialidades activas |
| `GET`   | `/specialties/catalog`              | Catálogo de necesidades + reglas de matching |
| `POST/PATCH/DELETE` | `/specialties` / `/specialties/{id}` | CRUD de especialidades (admin/super_admin) |
| `POST`  | `/consultations/{id}/close`         | Cerrar / no-show (+ evento de auditoría) |
| `POST`  | `/consultations/{id}/heartbeat`     | Presencia del paciente (sala de espera) |
| `POST`  | `/consultations/{id}/video-room`    | Sala Jitsi idempotente (espera o atención) |
| `POST`  | `/consultations/{id}/claim`         | Tomar un caso de la cola (crea la sala en el mismo UPDATE) |
| `GET`   | `/consultations/derivation-targets` | Especialidades con médicos para derivar |
| `POST`  | `/consultations/{id}/derive`        | Derivar un caso de la cola a otra especialidad |
| `POST`  | `/consultations/{id}/refer-to-queue`| Derivar con especialista (firmado, sin cita) |
| `GET`   | `/consultations/{id}/waiting-room`  | Estado de la sala de espera del paciente |
| `GET`   | `/consultations/{id}/waiting-room/stream` | Lo mismo por SSE             |
| `GET`   | `/consultations/{id}/messages`       | Hilo de mensajes de la consulta: `{ consultation_id, unread_count, items[], clinical_access }`. Grant fail-closed (sin él, `body` y `file_name` en null); paginado con `limit`/`offset` y recorte por `after_id`/`before_id` |
| `POST`  | `/consultations/{id}/messages`       | Enviar mensaje: `{ body?, attachment_ids?, client_msg_id? }` (hace falta uno de los dos primeros). Idempotente por `client_msg_id` (200 si ya existía); 409 si la consulta no admite mensajes; rate limit `PUBLIC_WRITE_RATE_LIMIT` |
| `POST`  | `/consultations/{id}/messages/read`  | Marcar leídos los de la otra dirección → `{ marked: n }`. Idempotente (escritura condicional) |
| `POST`  | `/consultations/{id}/attachments`    | Subir adjunto clínico `multipart/form-data` (PDF, JPG, PNG, WEBP; **GIF prohibido**, 422). Valida magic bytes y 10 MB; va al bucket privado `chat-attachments` |
| `GET`   | `/consultations/{id}/attachments/{attachment_id}` | Descargar el adjunto (`inline`, `nosniff`). Exige pertenencia y audita `READ_CLINICAL_DATA`; un admin que no es el tratante recibe 403 |
| `GET`   | `/inbox`                            | Buzón del médico: un hilo por consulta donde es o fue tratante, con `unread_count`, `last_message_at` y la presencia del paciente. Filtros `only_unread`, `limit`, `offset` |
| `GET`   | `/inbox/stream`                     | Lo mismo por SSE: evento `inbox` `{ unread_total, updated[] }`, sin cuerpos |
| `GET`   | `/doctors/specialty-requests`       | Especialidades escritas por médicos (admin) |
| `POST`  | `/doctors/{id}/specialty-request/resolve` | Asignarle una del catálogo (admin) |
| `POST`  | `/profiles/{id}/online`             | Presencia del médico (`last_seen_at`) |
| `PATCH` | `/profiles/{id}/active`             | Revocar / reactivar médico (admin)   |
| `POST`  | `/profiles/{id}/finalize-role`      | Finalizar rol (patient/doctor)       |
| `GET`   | `/patients`                         | Lista pacientes                      |
| `POST`  | `/patients`                         | Crea paciente (requiere `consent`)   |
| `GET`   | `/patients/{id}`                    | Detalle                              |
| `PATCH` | `/patients/{id}`                    | Actualiza                            |
| `DELETE`| `/patients/{id}`                    | Elimina                              |
| `GET`   | `/consultations?status=&patient_id=`| Lista consultas (filtros)            |
| `POST`  | `/consultations`                    | Crea consulta (código autogenerado)  |
| `GET`   | `/consultations/{id}`               | Detalle                              |
| `PATCH` | `/consultations/{id}`               | Actualiza estado/asignación/notas    |
| `GET`   | `/consultations/{id}/events`        | Eventos de la consulta               |
| `POST`  | `/consultations/{id}/events`        | Registra evento                      |
| `GET`   | `/doctors?status=`                  | Lista médicos                        |
| `POST`  | `/doctors`                          | Crea médico                          |
| `GET`   | `/doctors/{id}` · `PATCH` · `DELETE`| Detalle / actualizar / eliminar      |
| `GET`   | `/profiles?role=`                   | Lista perfiles (solo lectura)        |
| `GET`   | `/profiles/{id}`                    | Detalle de perfil                    |
| `GET`   | `/roles`                            | Catálogo de roles (`roles.assign`)   |
| `GET`   | `/users/{id}/roles`                 | Roles activos de un usuario (`roles.assign`) |
| `POST`  | `/users/{id}/roles`                 | Asignar rol (auditado; `roles.assign`; otorgar `super_admin` exige actor `super_admin`) |
| `DELETE`| `/users/{id}/roles/{role_id}`       | Revocar rol (soft, auditado; `roles.assign`) |
| `POST`  | `/users`                            | Crear usuario de Auth + rol inicial opcional (auditado; `users.create`) |
| `GET`   | `/audit-log?action=&actor_user_id=&resource=` | Registro de auditoría (`audit.read`) |
| `GET`   | `/reports/doctors` · `/reports/patients` | Vista previa paginada del reporte, con las columnas y el `total` que traería el Excel (`reports.export`) |
| `GET`   | `/reports/doctors/export` · `/reports/patients/export` | El reporte completo en `.xlsx` (mismos filtros, sin `limit`; auditado) |
| `POST`  | `/marketing/surveys/{survey}/responses` | Responder una encuesta de marketing (**público**, rate limit `SURVEY_RESPONSE_RATE_LIMIT`). `survey` = `psicologos` · `especialistas` · `medicos-generales`; responder de nuevo con el mismo correo reemplaza la respuesta |
| `GET`   | `/marketing/surveys/{survey}/responses` · `.../export` | Respuestas de una encuesta: vista previa paginada y `.xlsx` auditado (`marketing.read`) |
| `GET`   | `/marketing/surveys` | Total de respuestas de cada encuesta, para las pestañas del panel (`marketing.read`) |
| `GET`   | `/marketing/performance` | Embudo de cada encuesta: enviados, aperturas, clics y bajas (Kit, en caché unos minutos) + respuestas, y línea de tiempo de las respuestas desde el primer envío (`marketing.read`) |
| `GET`   | `/marketing/surveys/{survey}/stats` | Agregados para los gráficos: cobertura día × momento, formas de participar, horas (y horas mínimas), ubicación; filtros de fecha y `role` (`marketing.read`) |

## Concurrencia: toma de cola anti-colisión

`POST /queue/{id}/take` protege la integridad de la cola cuando dos médicos eligen el
mismo paciente a la vez. El servicio usa bloqueo pesimista de **fallo rápido**:

```python
select(Consultation).where(...status == "waiting").with_for_update(nowait=True)
```

- El **ganador** obtiene `200` y la consulta pasa a `in_progress`.
- El **perdedor** obtiene `409` (fila bloqueada, `LockNotAvailableError` / SQLSTATE `55P03`)
  o `404` si la carrera se resolvió justo tras el commit del ganador.
- Nunca hay doble asignación ni peticiones colgadas.

## Tests

```bash
uv run pytest --cov=src --cov-report=term-missing
```

- Aislamiento por **savepoints** (cada test mutativo hace rollback automático).
- Prueba de **colisión concurrente** con `asyncio.gather` + `httpx.AsyncClient`
  (un ganador 200, el resto 409/404; sin doble asignación).
- Cobertura actual: **97%** (objetivo ≥95%).
- Requiere el Postgres local corriendo (`docker compose up -d db`).

## Migración del frontend: Supabase → esta API

El objetivo es que la app Next.js haga **exactamente lo mismo** que hoy hace contra Supabase,
pero llamando a esta API. Equivalencias de las operaciones de datos:

| Operación actual (Supabase)                                  | Endpoint de esta API |
| ------------------------------------------------------------ | -------------------- |
| `insert patients`                                            | `POST /patients` |
| `insert consultations` (status waiting, priority derivada)   | `POST /consultations` (deriva categoría/prioridad del paciente) |
| update atómico de toma (`.eq('status','waiting')`)           | `POST /queue/{id}/take` · `POST /queue/attend-next` |
| update cierre / no-show + `addEvent`                         | `POST /consultations/{id}/close` |
| `insert consultation_events`                                 | `POST /consultations/{id}/events` |
| `rpc('mark_patient_waiting')`                                | `POST /consultations/{id}/heartbeat` |
| `rpc('mark_myself_online')`                                  | `POST /profiles/me/online` |
| `rpc('set_my_role')`                                         | `POST /profiles/me/finalize-role` |
| `update profiles set active` (revocar)                       | `PATCH /profiles/{id}/active` |
| `/api/videoconsulta` (sala Jitsi idempotente)                | `POST /consultations/{id}/video-room` |
| `getSession` + cargar profile                                | `GET /auth/me` |
| `select` (cola, detalle, mi-caso, dashboard)                 | `GET /queue`, `GET /consultations`, `GET /consultations/{id}`, `GET /profiles` |
| `SPECIALTY_NEEDS` / `canAttend` (lib/utils.ts)               | `GET /specialties/catalog` + ya aplicado server-side en `attend-next` |

> **Autenticación:** el login sigue en **Supabase Auth** (email/password + Google OAuth); el
> frontend manda el **JWT de Supabase** y la API lo valida + aplica RBAC (ver sección de arriba y
> `@.claude/rules/security.md`). Las acciones de médico (tomar/cerrar/presencia) derivan el actor del
> token, así que esas llamadas ya **no** mandan `assigned_doctor_id`/`closed_by`.

## Notas

- En local **no** se usa RLS: la autorización la impone la **capa de servicios/RBAC** (replica las
  políticas RLS). Las políticas RLS de Supabase se restauran pero no aplican al conectarse como dueño.
- **Observabilidad:** cada respuesta lleva `X-Correlation-ID` y los logs salen en **JSON** (logger
  `mpv.api`); prohibido `print()`. **Paginación:** las listas usan `limit`/`offset` (máx. 100).
- **Resiliencia:** `POST /queue/release-stale` devuelve a la cola las consultas `in_progress`
  abiertas hace más de `STALE_CONSULTATION_MINUTES`; pensado para un CRON/worker.
- El `code` de las consultas lo genera **siempre** el trigger `generate_consultation_code`
  (cualquier valor enviado por el cliente se ignora).
- Las tablas `prescriptions`, `referrals`, `rest_notes`, `treatment_plans`, `follow_ups`,
  `messages` y `admin_users` están **modeladas**; sus endpoints se pueden añadir según se
  necesiten.
