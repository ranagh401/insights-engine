"""Default JWT FastAPI dependency (overridden by ``insights_engine`` app factory)."""


async def get_jwt_manager():
    """Resolved at runtime after the app registers dependency overrides."""
    from insights_engine.config import get_settings

    return await get_settings().get_jwt_manager()
