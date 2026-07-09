"""Check signal_log for wow_growth_pct and prev_kpi_value correctness."""
import asyncio
import os
from dotenv import load_dotenv
import asyncpg

load_dotenv()


def _clean_dsn(raw: str) -> str:
    return raw.replace("postgresql+asyncpg://", "postgresql://").replace("postgres+asyncpg://", "postgresql://")


PG = _clean_dsn(
    os.environ.get("POSTGRES_DSN")
    or os.environ.get("ONEPLATFORM_DATABASE__URL")
    or os.environ.get("POSTGRES_URI")
    or os.environ.get("DATABASE_URL", "")
)

QUERY = """
SELECT
    dimension_value,
    feature_name,
    ROUND(feature_value::numeric, 4)   AS feature_value,
    ROUND(current_kpi_value::numeric, 4) AS current_kpi,
    ROUND(prev_kpi_value::numeric, 4)    AS prev_kpi,
    signal_name,
    LEFT(dax_feature_query, 120)       AS feat_query_snippet
FROM insights.signal_log
WHERE feature_name = 'wow_growth_pct'
ORDER BY dimension_value
LIMIT 20;
"""


async def main() -> None:
    conn = await asyncpg.connect(PG)
    rows = await conn.fetch(QUERY)
    await conn.close()
    if not rows:
        print("No wow_growth_pct signals found in signal_log.")
        return
    header = f"{'Branch':<30} {'feat_val':>10} {'cur_kpi':>10} {'prev_kpi':>10} {'signal':<25}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['dimension_value']:<30} "
            f"{str(r['feature_value']):>10} "
            f"{str(r['current_kpi']):>10} "
            f"{str(r['prev_kpi']):>10} "
            f"{r['signal_name']:<25}"
        )
    print()
    print("Sample dax_feature_query snippet (first row):")
    if rows:
        print(rows[0]["feat_query_snippet"])

    # Also print Charlotte specifically
    q2 = """
    SELECT dimension_value, feature_name, feature_value, current_kpi_value, prev_kpi_value,
           dax_feature_query
    FROM insights.signal_log
    WHERE dimension_value = 'Charlotte' AND feature_name = 'wow_growth_pct'
    LIMIT 1;
    """
    conn = await asyncpg.connect(PG)
    ch_rows = await conn.fetch(q2)
    await conn.close()
    if ch_rows:
        r = ch_rows[0]
        print(f"\nCharlotte wow_growth_pct:")
        print(f"  current_kpi_value: {r['current_kpi_value']}")
        print(f"  prev_kpi_value:    {r['prev_kpi_value']}")
        print(f"  feature_value:     {r['feature_value']}")
        expected = None
        if r['current_kpi_value'] and r['prev_kpi_value'] and r['prev_kpi_value'] != 0:
            expected = (float(r['current_kpi_value']) - float(r['prev_kpi_value'])) / abs(float(r['prev_kpi_value'])) * 100
            print(f"  expected wow%:     {expected:.4f}")
            match = abs(float(r['feature_value']) - expected) < 0.01 if r['feature_value'] else False
            print(f"  feature_value matches: {match}")
        print(f"\n  dax_feature_query:\n{r['dax_feature_query']}")


if __name__ == "__main__":
    asyncio.run(main())
