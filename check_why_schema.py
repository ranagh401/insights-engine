"""Check the current why_results table schema."""
import asyncio
import os
from dotenv import load_dotenv
import asyncpg

load_dotenv()


def _clean_dsn(raw: str) -> str:
    return raw.replace("postgresql+asyncpg://", "postgresql://").replace("postgres+asyncpg://", "postgresql://")


PG = _clean_dsn(
    os.environ.get("POSTGRES_DSN")
    or os.environ.get("PLATFORM_DATABASE__URL")
    or os.environ.get("POSTGRES_URI")
    or os.environ.get("DATABASE_URL", "")
)


async def main() -> None:
    conn = await asyncpg.connect(PG)
    rows = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'insights' AND table_name = 'why_results' "
        "ORDER BY ordinal_position"
    )
    await conn.close()
    print("why_results columns:")
    for r in rows:
        print(f"  {r['column_name']:<25} {r['data_type']}")


if __name__ == "__main__":
    asyncio.run(main())
