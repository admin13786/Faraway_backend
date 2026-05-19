#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="${APP_ROOT:-$HOME/faraway-project}"
BACKEND_REPO="${BACKEND_REPO:-git@github.com:admin13786/Faraway_backend.git}"
WEB_REPO="${WEB_REPO:-git@github.com:admin13786/Faraway_fronted.git}"
BACKEND_BRANCH="${BACKEND_BRANCH:-main}"
WEB_BRANCH="${WEB_BRANCH:-main}"

log() {
  printf '\n==> %s\n' "$1"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing command: $1" >&2
    exit 1
  fi
}

generate_secret_key() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  elif command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
  else
    {
      date +%s%N
      hostname
      ps aux 2>/dev/null || true
    } | sha256sum | awk '{print $1}'
  fi
}

set_env_var() {
  local file="$1"
  local key="$2"
  local value="$3"
  local escaped
  escaped="$(printf '%s' "$value" | sed 's/[\/&]/\\&/g')"
  if grep -qE "^${key}=" "$file"; then
    sed -i "s/^${key}=.*/${key}=\"${escaped}\"/" "$file"
  else
    printf '%s="%s"\n' "$key" "$value" >>"$file"
  fi
}

ensure_backend_env() {
  local env_file="$BACKEND_DIR/.env"
  if [ ! -f "$env_file" ]; then
    if [ -f "$BACKEND_DIR/.env.example" ]; then
      cp "$BACKEND_DIR/.env.example" "$env_file"
    else
      touch "$env_file"
    fi
  fi

  local current_secret
  current_secret="$(grep -E '^SECRET_KEY=' "$env_file" | tail -n 1 | cut -d= -f2- | tr -d "\"'" || true)"
  case "$current_secret" in
    ""|"change-this-in-production-please-use-a-long-random-secret-key"|"change-this-before-deploying-faraway-backend"|"faraway-local-dev-secret-change-me")
      set_env_var "$env_file" "SECRET_KEY" "$(generate_secret_key)"
      log "Generated production SECRET_KEY in backend .env"
      ;;
    *)
      if [ "${#current_secret}" -lt 32 ]; then
        set_env_var "$env_file" "SECRET_KEY" "$(generate_secret_key)"
        log "Replaced weak SECRET_KEY in backend .env"
      fi
      ;;
  esac
}

wait_for_backend() {
  log "Waiting for backend health"
  local attempt
  for attempt in $(seq 1 30); do
    if compose exec -T backend python - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3).read()
PY
    then
      log "Backend is healthy"
      return 0
    fi
    sleep 2
  done

  echo "Backend did not become healthy in time" >&2
  compose logs --tail 120 backend || true
  exit 1
}

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    docker-compose "$@"
  fi
}

assert_safe_app_root() {
  if [ -z "$APP_ROOT" ]; then
    echo "APP_ROOT is empty; refuse to deploy" >&2
    exit 1
  fi
  case "$APP_ROOT" in
    /*) ;;
    *)
      echo "APP_ROOT must be an absolute path: $APP_ROOT" >&2
      exit 1
      ;;
  esac

  local resolved_app_root
  local resolved_home
  resolved_app_root="$(mkdir -p "$APP_ROOT" && cd "$APP_ROOT" && pwd -P)"
  resolved_home="$(cd "$HOME" && pwd -P)"

  case "$resolved_app_root" in
    "/"|"/home"|"$resolved_home")
      echo "Unsafe APP_ROOT: $resolved_app_root. Use a dedicated project directory such as $resolved_home/faraway-project" >&2
      exit 1
      ;;
  esac

  case "$resolved_app_root" in
    "$resolved_home"/*) ;;
    *)
      echo "APP_ROOT must stay under the current user's home directory: $resolved_app_root" >&2
      exit 1
      ;;
  esac

  APP_ROOT="$resolved_app_root"
}

clone_or_update() {
  local repo="$1"
  local branch="$2"
  local dir="$3"
  local expected="$4"

  case "$dir" in
    "$APP_ROOT"/"$expected") ;;
    *)
      echo "Unsafe target directory: $dir" >&2
      exit 1
      ;;
  esac

  if [ -d "$dir/.git" ]; then
    log "Updating $expected"
    git -C "$dir" fetch origin "$branch"
    git -C "$dir" checkout "$branch"
    git -C "$dir" reset --hard "origin/$branch"
  else
    log "Cloning $expected"
    rm -rf "$dir"
    git clone --branch "$branch" "$repo" "$dir"
  fi
}

require_cmd git
require_cmd docker
assert_safe_app_root

BACKEND_DIR="$APP_ROOT/backend"
WEB_DIR="$APP_ROOT/web_frontend"

log "Deploy root: $APP_ROOT"
mkdir -p "$APP_ROOT/.backup"
if [ -d "$BACKEND_DIR/data" ]; then
  cp -a "$BACKEND_DIR/data" "$APP_ROOT/.backup/backend-data-$(date +%Y%m%d%H%M%S)"
fi

clone_or_update "$BACKEND_REPO" "$BACKEND_BRANCH" "$BACKEND_DIR" "backend"
clone_or_update "$WEB_REPO" "$WEB_BRANCH" "$WEB_DIR" "web_frontend"

mkdir -p "$BACKEND_DIR/data"
ensure_backend_env
if [ ! -f "$WEB_DIR/.env" ] && [ -f "$WEB_DIR/.env.example" ]; then
  cp "$WEB_DIR/.env.example" "$WEB_DIR/.env"
fi

log "Ensuring shared docker network"
docker network inspect faraway-net >/dev/null 2>&1 || docker network create faraway-net

log "Building and restarting backend"
cd "$BACKEND_DIR"
compose up -d --build backend
wait_for_backend

if [ -f "scripts/seed_demo_match_pool.py" ]; then
  compose exec -T backend python scripts/seed_demo_match_pool.py || true
fi
if [ -f "scripts/fix_city_cover_images.py" ]; then
  compose exec -T backend python scripts/fix_city_cover_images.py || true
fi

log "Building and restarting web"
cd "$WEB_DIR"
compose up -d --build web

log "Faraway containers"
docker ps --filter "name=faraway-backend" --filter "name=faraway-web"
