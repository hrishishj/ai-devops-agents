"""Thin ingress API: verify -> normalise -> persist -> enqueue -> 202. No agent work runs here."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from devopsagents.apps.api.routes import admin, alerts, github, health
from devopsagents.apps.container import Container, build_container
from devopsagents.core.config.settings import get_settings
from devopsagents.core.telemetry.tracing import configure_logging


def create_app(container: Container | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if container is None:
            configure_logging()
            app.state.container = build_container(get_settings(), base_dir=Path.cwd())
        yield

    app = FastAPI(
        title="AI DevOps Agents", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None
    )  # no public docs surface; CORS intentionally off
    if container is not None:
        app.state.container = container
    for r in (github.router, alerts.router, admin.router, health.router):
        app.include_router(r)
    return app


def factory() -> FastAPI:
    """`uvicorn devopsagents.apps.api.main:factory --factory`"""
    return create_app()
