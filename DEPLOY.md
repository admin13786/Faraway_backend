# Faraway Backend Deployment

This repository can be built directly on a Linux cloud server with Docker.

## Build and run

```bash
git clone https://github.com/admin13786/Faraway_backend.git
cd Faraway_backend
cp .env.example .env
docker compose up -d --build
```

Health check:

```bash
curl http://127.0.0.1:8011/health
```

The backend container joins the external Docker network name `faraway-net`.
The web frontend repository uses the container name `faraway-backend:8000` as its upstream.

## Required production settings

Edit `.env` before production use:

```env
SECRET_KEY=replace-with-a-long-random-secret
DASHSCOPE_API_KEY=
SEED_DEMO_DATA=true
BACKEND_PORT=8011
PYTHON_IMAGE=python:3.10-slim
```

Do not commit `.env`, SQLite databases, logs, or uploaded media.
