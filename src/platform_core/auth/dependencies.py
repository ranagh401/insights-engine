"""Default JWT FastAPI dependency (overridden by ``ms_crm_insights_portal`` app factory)."""


async def get_jwt_manager():
    """Resolved at runtime after the app registers dependency overrides."""
    from ms_crm_insights_portal.config import get_settings

    return await get_settings().get_jwt_manager()
