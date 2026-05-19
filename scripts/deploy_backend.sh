#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="${APP_ROOT:-$HOME/faraway-project}"
BACKEND_DIR="$APP_ROOT/backend"
RELEASE_TAR="${1:-$APP_ROOT/backend-release.tar.gz}"

log() {
  printf '\n==> %s\n' "$1"
}

require_file() {
  if [ ! -f "$1" ]; then
    echo "Missing file: $1" >&2
    exit 1
  fi
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
    "/"|"/home"|"$resolved_home"|"$resolved_home/"*)
      if [ "$resolved_app_root" != "$resolved_home/faraway-project" ]; then
        echo "Unsafe APP_ROOT: $resolved_app_root. Use a dedicated project directory such as $resolved_home/faraway-project" >&2
        exit 1
      fi
      ;;
  esac
  APP_ROOT="$resolved_app_root"
  BACKEND_DIR="$APP_ROOT/backend"
  RELEASE_TAR="${1:-$APP_ROOT/backend-release.tar.gz}"
}

assert_safe_child_dir() {
  local child="$1"
  local resolved_child
  resolved_child="$(mkdir -p "$child" && cd "$child" && pwd -P)"
  case "$resolved_child" in
    "$APP_ROOT"/backend) ;;
    *)
      echo "Unsafe backend directory: $resolved_child" >&2
      exit 1
      ;;
  esac
  BACKEND_DIR="$resolved_child"
}

assert_safe_app_root "$@"
assert_safe_child_dir "$BACKEND_DIR"
require_cmd docker
require_file "$RELEASE_TAR"

log "Preparing backend directory: $BACKEND_DIR"
mkdir -p "$BACKEND_DIR"

if [ -d "$BACKEND_DIR/data" ]; then
  mkdir -p "$APP_ROOT/.backup"
  cp -a "$BACKEND_DIR/data" "$APP_ROOT/.backup/backend-data-$(date +%Y%m%d%H%M%S)"
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
tar -xzf "$RELEASE_TAR" -C "$TMP_DIR"

if [ ! -f "$TMP_DIR/Dockerfile" ] || [ ! -f "$TMP_DIR/docker-compose.yml" ]; then
  echo "Release package is not a backend repository archive" >&2
  exit 1
fi

log "Syncing backend source"
find "$BACKEND_DIR" -mindepth 1 -maxdepth 1 ! -name data ! -name .env -exec rm -rf {} +
cp -a "$TMP_DIR"/. "$BACKEND_DIR"/
mkdir -p "$BACKEND_DIR/data"

ensure_backend_env

log "Building and restarting backend only"
cd "$BACKEND_DIR"
compose up -d --build backend

if [ -f "scripts/seed_demo_match_pool.py" ]; then
  compose exec -T backend python scripts/seed_demo_match_pool.py || true
fi
if [ -f "scripts/fix_city_cover_images.py" ]; then
  compose exec -T backend python scripts/fix_city_cover_images.py || true
fi

log "Backend status"
compose ps backend
docker inspect --format='{{.State.Health.Status}}' faraway-backend 2>/dev/null || true
