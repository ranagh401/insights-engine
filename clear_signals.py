"""Utility: clear signal_log table before a fresh test run."""
import asyncio
import os
from dotenv import load_dotenv
import asyncpg

load_dotenv()

def _clean_dsn(raw: str) -> str:
    """Strip SQLAlchemy driver prefix so asyncpg can parse the DSN."""
    return raw.replace("postgresql+asyncpg://", "postgresql://").replace("postgres+asyncpg://", "postgresql://")


PG = _clean_dsn(
    os.environ.get("POSTGRES_DSN")
    or os.environ.get("ONEPLATFORM_DATABASE__URL")
    or os.environ.get("POSTGRES_URI")
    or os.environ.get("DATABASE_URL", "")
)


async def main() -> None:
    conn = await asyncpg.connect(PG)
    count = await conn.fetchval("SELECT COUNT(*) FROM insights.signal_log")
    await conn.execute("DELETE FROM insights.signal_log")
    await conn.close()
    print(f"Deleted {count} rows from insights.signal_log")


if __name__ == "__main__":
    asyncio.run(main())
