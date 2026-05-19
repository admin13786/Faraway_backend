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

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    docker-compose "$@"
  fi
}

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

if [ ! -f "$BACKEND_DIR/.env" ]; then
  if [ -f "$BACKEND_DIR/.env.example" ]; then
    cp "$BACKEND_DIR/.env.example" "$BACKEND_DIR/.env"
  else
    touch "$BACKEND_DIR/.env"
  fi
fi

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
