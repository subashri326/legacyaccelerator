"""FastAPI application entrypoint with lifespan init, exception handlers, and /metrics."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from accelerator.api.routers import (
    auth,
    connections,
    migrations,
    objects,
    plugins,
    projects,
    review,
    stages,
    track,
)
from accelerator.config.settings import Settings, get_settings
from accelerator.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigError,
    EvidenceRequiredError,
    NotFoundError,
    PluginNotFoundError,
    StateTransitionError,
    UnsafePathError,
)
from accelerator.core.observability import configure_logging, metrics
from accelerator.db.session import init_db, session_scope
from accelerator.engines.reconcile import purge_expired_samples
from accelerator.orchestration.live_runs import mark_interrupted_runs
from accelerator.plugins.registry import get_plugin_registry


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    init_db()
    with session_scope() as db:
        purge_expired_samples(db)
        get_plugin_registry().sync_to_db(db)
    mark_interrupted_runs()
    yield


DEV_JWT_SECRET = "dev-insecure-secret-change-for-production-32bytes"


def check_production_settings(settings: Settings) -> None:
    """Refuse to start outside dev with settings that are only safe on a laptop."""
    if settings.is_dev:
        return
    problems = []
    if settings.auth_mode == "local" and (
        settings.jwt_secret == DEV_JWT_SECRET or len(settings.jwt_secret) < 32
    ):
        problems.append("L2C_JWT_SECRET must be set to a random value of at least 32 characters")
    if settings.auth_mode == "oidc" and not (settings.oidc_issuer and settings.oidc_audience):
        problems.append(
            "L2C_OIDC_ISSUER and L2C_OIDC_AUDIENCE are required when L2C_AUTH_MODE=oidc"
        )
    if "*" in settings.cors_allowed_origins:
        problems.append("L2C_CORS_ALLOWED_ORIGINS must list the UI origins explicitly, not '*'")
    if problems:
        raise RuntimeError(
            f"Unsafe configuration for environment '{settings.environment}': " + "; ".join(problems)
        )


def create_app() -> FastAPI:
    settings = get_settings()
    check_production_settings(settings)
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,  # bearer tokens, no cookies
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(AuthenticationError)
    async def _auth_err(_req: Request, exc: AuthenticationError) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.exception_handler(AuthorizationError)
    async def _authz_err(_req: Request, exc: AuthorizationError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(ConfigError)
    async def _cfg_err(_req: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(EvidenceRequiredError)
    async def _ev_err(_req: Request, exc: EvidenceRequiredError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(StateTransitionError)
    async def _state_err(_req: Request, exc: StateTransitionError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(PluginNotFoundError)
    async def _plugin_err(_req: Request, exc: PluginNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(NotFoundError)
    async def _nf_err(_req: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(UnsafePathError)
    async def _path_err(_req: Request, exc: UnsafePathError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def _val_err(_req: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/healthz", tags=["platform"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", tags=["platform"])
    def prometheus_metrics() -> Response:
        return Response(content=metrics.render_prometheus(), media_type="text/plain; version=0.0.4")

    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(stages.router)
    app.include_router(objects.router)
    app.include_router(review.router)
    app.include_router(track.router)
    app.include_router(plugins.router)
    app.include_router(connections.router)
    app.include_router(migrations.router)
    mount_web_app(app)
    return app


def web_app_dir() -> Path | None:
    """The built React app (``frontend/dist``), if it has been built.

    ``L2C_UI_DIR`` points somewhere else; set it to an empty string to serve the API only.
    """
    configured = os.environ.get("L2C_UI_DIR")
    if configured is not None:
        path = Path(configured) if configured else None
    else:
        path = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    return path if path and (path / "index.html").is_file() else None


def mount_web_app(app: FastAPI) -> None:
    """Serve the React app from the same address as the API, so one command runs everything.

    Any path that is not the API and not a real file returns index.html, so links such as
    /migrations/123 work when opened directly or refreshed.
    """
    root = web_app_dir()
    if root is None:
        return
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="web-assets")
    index = root / "index.html"
    resolved_root = root.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def web_app(path: str) -> Response:
        if path.startswith(("api/", "healthz", "metrics")):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(resolved_root):
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
