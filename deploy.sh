#!/usr/bin/env bash
# =====================================================================
# Deploy del backend en EC2 (Amazon Linux 2023).
#   Uso:   ./deploy.sh            (rama dev por defecto)
#          ./deploy.sh main       (otra rama)
#          ./deploy.sh dev -y      (sin confirmación de backup)
#
# Orden pensado para expand/contract SIN downtime:
#   1) git pull   2) build imagen nueva   3) migrar desde un contenedor efímero
#   (la app VIEJA sigue sirviendo; tras un rename, la vista de compat la cubre)
#   4) swap de la app a la imagen nueva   5) health check
#
# Notas del entorno: docker sin sudo (usuario en grupo docker); se usa `docker build`
# y no `docker compose --build` porque el buildx del host es < 0.17.
# =====================================================================
set -euo pipefail

COMPOSE="docker-compose.prod.yml"
ENV_FILE=".env.production"
IMAGE="api-medicos-por-venezuela"
# localhost es lo robusto (el script corre en el propio EC2; no depende de la IP
# pública ni del Security Group). El 8000 solo escucha en 127.0.0.1: desde fuera no
# responde, se entra por Caddy (ver docs/proxy-e-ip-real.md).
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/api/v1/health}"
# El mismo health pero a través de Caddy: si el upstream de Caddy no es 127.0.0.1:8000,
# el de arriba pasa y la API está caída para el público. PUBLIC_HEALTH_URL= lo salta.
PUBLIC_HEALTH_URL="${PUBLIC_HEALTH_URL-https://api.medicosporvenezuela.org/api/v1/health}"

BRANCH="dev"
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    *) BRANCH="$arg" ;;
  esac
done

cd "$(dirname "$0")"

# --- Pre-check: archivos y backup ---
[ -f "$COMPOSE" ] || { echo "ERROR: no encuentro $COMPOSE (¿estás en el repo?)" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "ERROR: falta $ENV_FILE en el EC2." >&2; exit 1; }

# --- Pre-check: variables que la app exige para arrancar ---
# Sin estas, uvicorn levanta y se muere en el lifespan (src/main.py las valida), así que el
# deploy fallaría recién en el health check del paso 5/5 — después de construir la imagen y de
# aplicar migraciones a producción. Mejor caerse aquí, antes de tocar nada.
# `grep -q` sobre el archivo y no `source`: no se cargan los secretos en este shell ni se
# imprimen nunca (ver .claude/rules/security.md).
for var in SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY; do
  if ! grep -Eq "^[[:space:]]*${var}=.+" "$ENV_FILE"; then
    echo "ERROR: falta $var en $ENV_FILE." >&2
    echo "   Supabase -> Project Settings -> API (la URL del proyecto y el 'service_role' secret)." >&2
    echo "   Las usan services/users.py (altas de Auth) y services/storage.py (adjuntos del chat)." >&2
    exit 1
  fi
done

# Los adjuntos clínicos del chat van a un bucket PRIVADO de Supabase Storage que NO se crea
# solo (el CLI solo lo declara para el Supabase local). Si falta, la primera subida de un PDF
# responde 502. No se puede verificar desde aquí sin pegarle a la API con el secreto, así que
# queda como recordatorio: una vez por proyecto, y listo.
echo "ℹ️  Adjuntos del chat: el bucket PRIVADO 'chat-attachments' debe existir en Supabase"
echo "   (Storage -> New bucket, 'Public bucket' DESMARCADO). Ver README -> 'Adjuntos del chat'."

if [ "$ASSUME_YES" -ne 1 ]; then
  echo "⚠️  Esto aplica migraciones a la Supabase de PRODUCCIÓN."
  read -r -p "¿Tenés un backup reciente? [y/N] " ok
  [ "$ok" = "y" ] || [ "$ok" = "Y" ] || { echo "Abortado. Hacé el backup y reintentá."; exit 1; }
fi

echo "==> 1/5 git pull ($BRANCH)"
git pull origin "$BRANCH"

echo "==> 2/5 build de la imagen"
docker build -t "$IMAGE" .

echo "==> 3/5 migraciones (contenedor efímero desde la imagen nueva)"
docker compose -f "$COMPOSE" run --rm api python artisan migrate

echo "==> 4/5 swap de la app a la imagen nueva"
docker compose -f "$COMPOSE" up -d

echo "==> 5/5 health check"
for i in $(seq 1 15); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)"
  [ "$code" = "200" ] && break
  sleep 2
done

if [ "${code:-}" = "200" ] && [ -n "$PUBLIC_HEALTH_URL" ]; then
  public_code="$(curl -s -o /dev/null -w '%{http_code}' "$PUBLIC_HEALTH_URL" || true)"
  if [ "$public_code" != "200" ]; then
    echo "❌ ERROR — la API responde en localhost pero por Caddy devuelve '${public_code:-sin respuesta}'." >&2
    echo "   ¿El reverse_proxy de Caddy apunta a 127.0.0.1:8000? Ver docs/proxy-e-ip-real.md" >&2
    exit 1
  fi
fi

if [ "${code:-}" = "200" ]; then
  echo "✅ OK — health 200 (localhost y Caddy). Deploy completo."
  docker compose -f "$COMPOSE" exec -T api python artisan migrate:status | tail -n 12
else
  echo "❌ ERROR — health devolvió '${code:-sin respuesta}'." >&2
  echo "   Logs: docker compose -f $COMPOSE logs --tail=50 api" >&2
  exit 1
fi
