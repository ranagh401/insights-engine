"""FastAPI application factory aligned with production ``create_fastapi_app`` signature."""
from typing import Any, Callable, List, Optional

from fastapi import APIRouter, FastAPI


def create_fastapi_app(
    *,
    settings: Any,
    title: str,
    description: str,
    routers: List[dict],
    lifespan: Optional[Callable[[Any], Any]] = None,
    enable_docs: bool = True,
    enable_health_endpoints: bool = False,
) -> FastAPI:
    """
    Build a FastAPI app and mount versioned API routers under ``/api/v1``.

    Parameters mirror the internal platform helper used in production.
    """
    _ = settings
    app = FastAPI(title=title, description=description, lifespan=lifespan)
    if not enable_docs:
        app.docs_url = None
        app.redoc_url = None
        app.openapi_url = None

    @app.get("/", tags=["Root"], include_in_schema=True)
    def root() -> dict:
        """Browser-friendly root; use 127.0.0.1 or localhost — not 0.0.0.0 in the address bar."""
        return {
            "service": title,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "api_v1": "/api/v1",
        }

    api = APIRouter()
    for spec in routers:
        api.include_router(spec["router"])
    app.include_router(api, prefix="/api/v1")

    if enable_health_endpoints:

        @app.get("/health", tags=["Health"])
        def health() -> dict:
            return {"status": "ok"}

    return app
