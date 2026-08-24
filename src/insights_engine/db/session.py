"""
Async SQLAlchemy session factory for the Insights PostgreSQL database.

Provides:
    engine              — the global async engine
    async_session_factory — sessionmaker bound to the engine
    get_db()            — FastAPI dependency that yields an AsyncSession

SQL echo is opt-in only (``INSIGHTS_LOG_SQL=1``). ``PLATFORM_DEBUG`` alone
does not print every statement, so signal pipelines stay readable.
"""
import os
import ssl

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config.config import get_settings


def _sqlalchemy_echo() -> bool:
    return os.environ.get("INSIGHTS_LOG_SQL", "").strip().lower() in ("1", "true", "yes")


_settings = get_settings()

# Azure PostgreSQL requires SSL; localhost does not
_connect_args = {}
_db_url_str = str(_settings.database.url)
# Ensure async driver is used
if _db_url_str.startswith("postgresql://"):
    _db_url_str = _db_url_str.replace("postgresql://", "postgresql+asyncpg://", 1)

if "azure" in _db_url_str or "amazonaws" in _db_url_str:
    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE
    _connect_args = {"ssl": _ssl_ctx}


def _pg_pool_kwargs() -> dict:
    """Async connection pool (defaults match prior SQLAlchemy 5+10; raise via env for heavy LLM + parallel DB)."""
    return {
        "pool_size": int(os.environ.get("INSIGHTS_PG_POOL_SIZE", "20")),
        "max_overflow": int(os.environ.get("INSIGHTS_PG_MAX_OVERFLOW", "30")),
        "pool_timeout": float(os.environ.get("INSIGHTS_PG_POOL_TIMEOUT", "60")),
    }


engine = create_async_engine(
    _db_url_str,
    echo=_sqlalchemy_echo(),
    pool_pre_ping=True,
    connect_args=_connect_args,
    **_pg_pool_kwargs(),
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db():
    """FastAPI dependency — yields an AsyncSession and commits/rolls back."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
