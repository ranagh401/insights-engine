"""Shared lifespan hook (no gRPC / Rabbit wiring in replica)."""
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, List, Optional


@asynccontextmanager
async def service_lifespan(
    app: Any,
    settings: Any,
    servicer_adders: Optional[List[Any]] = None,
) -> AsyncIterator[None]:
    """Yield control to the application after optional platform hooks."""
    if servicer_adders is None:
        servicer_adders = []
    _ = app
    _ = settings
    _ = servicer_adders
    yield
