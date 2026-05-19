from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.routers import frontend_api, health
from app.services.demo_seed import seed_demo_data_if_empty

app = FastAPI(
    title=settings.app_name,
    description="Faraway portable backend package",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

settings.resolved_local_media_dir.mkdir(parents=True, exist_ok=True)
if settings.seed_demo_data:
    seed_demo_data_if_empty()
app.mount(
    settings.local_media_url_prefix,
    StaticFiles(directory=settings.resolved_local_media_dir),
    name="media",
)

app.include_router(health.router)
app.include_router(frontend_api.router)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": str(exc.detail), "data": {}},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"code": 422, "message": "validation error", "data": {"errors": exc.errors()}},
    )
