"""
Standalone live DAX KPI compute test — reads config from Postgres.

Loads KPI measure names, valid dimensions, and PBI mappings from the
actual Renuity config tables.  No hardcoded KPI/dimension references.

USAGE
-----
1. Ensure .env and .env.dax are at the project root.
2. From the project root:

       $env:PYTHONPATH = "src"
       python -m tests.run_dax_kpi_test

   Override KPI (default = set_rate):
       $env:TEST_KPI = "demo_rate"
       python -m tests.run_dax_kpi_test

   Override date range:
       $env:TEST_START_DATE = "2026-03-16"
       $env:TEST_END_DATE = "2026-03-22"
       python -m tests.run_dax_kpi_test

   Five KPI compute-only runs (no signals, no WHY / dependencies):
       $env:PYTHONPATH = "src"
       $env:KPI_COMPUTE_BATCH = "1"
       python -m tests.run_dax_kpi_test

   Custom batch (comma-separated), still KPI-only if SKIP_SIGNALS_AND_WHY=1:
       $env:TEST_KPIS = "set_rate,demo_rate,raw_lead_count_mrk"
       $env:SKIP_SIGNALS_AND_WHY = "1"
       python -m tests.run_dax_kpi_test
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv

_root = Path(__file__).resolve().parents[1]
load_dotenv(_root / ".env")
load_dotenv(_root / ".env.dax")

# ── Test Parameters (overridable via env vars) ─────────────────────────────────

TEST_KPI = os.getenv("TEST_KPI", "set_rate")
PRODUCT_FILTER_ENABLED = os.getenv("PRODUCT_FILTER_ENABLED", "true").lower() in ("true", "1", "yes")
PRODUCT_FILTER_VALUES = os.getenv("PRODUCT_FILTER_VALUES", "Bath").split(",")

BREACH_THRESHOLD = float(os.getenv("BREACH_THRESHOLD", "30.0"))
BREACH_OPERATOR = os.getenv("BREACH_OPERATOR", "lt")

_start = os.getenv("TEST_START_DATE", "")
_end = os.getenv("TEST_END_DATE", "")
if _start and _end:
    TEST_START_DATE = date.fromisoformat(_start)
    TEST_END_DATE = date.fromisoformat(_end)
else:
    TEST_START_DATE = date(2026, 3, 16)
    TEST_END_DATE = date(2026, 3, 22)

DATE_TABLE = os.getenv("DATE_TABLE_NAME", "Calender")
DATE_COLUMN = os.getenv("DATE_COLUMN_NAME", "Calender Date")

SCHEMA = os.getenv("ONEPLATFORM_DATABASE__SCHEMA", "insights")
PG_DSN = os.getenv("ONEPLATFORM_DATABASE__URL", "")

AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")
PBI_WORKSPACE_ID = os.getenv("PBI_WORKSPACE_ID", "")
PBI_DATASET_ID = os.getenv("PBI_DATASET_ID", "")
PBI_MAX_CONCURRENT = int(os.getenv("PBI_MAX_CONCURRENT_QUERIES", "5"))
PBI_RETRIES = int(os.getenv("PBI_RETRY_MAX_ATTEMPTS", "3"))

# KPI-only mode: skip signal detection and WHY / dependency DAX calls
SKIP_SIGNALS_AND_WHY = os.getenv("SKIP_SIGNALS_AND_WHY", "").lower() in ("1", "true", "yes")
# Run 5 default KPI compute tests (implies SKIP_SIGNALS_AND_WHY unless overridden)
KPI_COMPUTE_BATCH = os.getenv("KPI_COMPUTE_BATCH", "").lower() in ("1", "true", "yes")
_DEFAULT_BATCH_KPIS = [
    "set_rate",
    "demo_rate",
    "issue_rate",
    "gross_close_rate",
    "net_close_rate",
]
_test_kpis_raw = os.getenv("TEST_KPIS", "").strip()
TEST_KPIS_BATCH: list[str] = (
    [x.strip() for x in _test_kpis_raw.split(",") if x.strip()]
    if _test_kpis_raw
    else (_DEFAULT_BATCH_KPIS if KPI_COMPUTE_BATCH else [])
)


def _section(title: str) -> None:
    print("\n" + "-" * 70)
    print(f"  {title}")
    print("-" * 70)


# ── Step 0: Load config from Postgres ──────────────────────────────────────────

async def load_kpi_config(kpi_name: str) -> dict[str, Any]:
    """Read KPI measure + valid dimensions + PBI mappings from Postgres."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(PG_DSN)
    config: dict[str, Any] = {"kpi_name": kpi_name}

    async with engine.begin() as conn:
        # Try base KPIs first, then derived
        r = await conn.execute(text(
            f"SELECT kpiname, pbi_measure_name, label "
            f"FROM {SCHEMA}.configkpisrenuitycrm WHERE kpiname = :kpi"
        ), {"kpi": kpi_name})
        row = r.fetchone()

        if not row:
            r = await conn.execute(text(
                f"SELECT kpiname, pbi_measure_name, label "
                f"FROM {SCHEMA}.configderivedkpisrenuitycrm WHERE kpiname = :kpi"
            ), {"kpi": kpi_name})
            row = r.fetchone()

        if not row:
            raise ValueError(f"KPI '{kpi_name}' not found in configkpisrenuitycrm or configderivedkpisrenuitycrm")
        if not row[1]:
            raise ValueError(f"KPI '{kpi_name}' has no pbi_measure_name. Run populate_pbi_columns.py first.")

        config["pbi_measure_name"] = row[1]
        config["label"] = row[2]

        # Valid dimensions with PBI mapping
        r2 = await conn.execute(text(
            f"SELECT vd.dimensionname, d.pbi_table_name, d.pbi_column_name "
            f"FROM {SCHEMA}.configkpivaliddimensionsrenuitycrm vd "
            f"JOIN {SCHEMA}.config_dimensionsreunitycrm d ON d.dimensionname = vd.dimensionname "
            f"WHERE vd.kpiname = :kpi AND vd.is_valid = true "
            f"AND d.pbi_table_name IS NOT NULL AND d.pbi_column_name IS NOT NULL "
            f"ORDER BY vd.dimensionname"
        ), {"kpi": kpi_name})
        dims = r2.fetchall()
        config["dimensions"] = [
            {"name": d[0], "pbi_table": d[1], "pbi_column": d[2]}
            for d in dims
        ]

        # Dependencies
        r3 = await conn.execute(text(
            f"SELECT dependent_kpi, pbi_measure_name, label "
            f"FROM {SCHEMA}.configkpidependenciesrenuitycrm "
            f"WHERE parent_kpi = :kpi AND pbi_measure_name IS NOT NULL "
            f"ORDER BY sort_order"
        ), {"kpi": kpi_name})
        deps = r3.fetchall()
        config["dependencies"] = [
            {"dependent_kpi": d[0], "pbi_measure": d[1], "label": d[2]}
            for d in deps
        ]

    await engine.dispose()
    return config


# ── Step 1: Build DAX query ────────────────────────────────────────────────────

def build_query(
    measure: str,
    dim: dict[str, str],
    product_dim: dict[str, str] | None = None,
) -> str:
    from ms_renuity_insights_portal.config.models import DimensionRef
    from ms_renuity_insights_portal.dax.query_builder import DAXQueryBuilder

    dim_ref = DimensionRef(
        dimension_name=dim["name"],
        pbi_table_name=dim["pbi_table"],
        pbi_column_name=dim["pbi_column"],
    )
    builder = (
        DAXQueryBuilder()
        .with_kpi(measure)
        .group_by(dim_ref)
        .add_date_filter(DATE_TABLE, DATE_COLUMN, TEST_START_DATE, TEST_END_DATE)
    )
    if PRODUCT_FILTER_ENABLED and product_dim:
        prod_ref = DimensionRef(
            dimension_name="Product",
            pbi_table_name=product_dim["pbi_table"],
            pbi_column_name=product_dim["pbi_column"],
        )
        builder = builder.add_member_filter(prod_ref, PRODUCT_FILTER_VALUES)
    return builder.build()


# ── Step 2: Execute DAX ───────────────────────────────────────────────────────

async def execute_dax(query: str) -> list[dict[str, Any]]:
    from ms_renuity_insights_portal.powerbi.api_client import PBIClient

    client = PBIClient(
        tenant_id=AZURE_TENANT_ID,
        client_id=AZURE_CLIENT_ID,
        client_secret=AZURE_CLIENT_SECRET,
        workspace_id=PBI_WORKSPACE_ID,
        dataset_id=PBI_DATASET_ID,
        max_concurrent=PBI_MAX_CONCURRENT,
        max_retries=PBI_RETRIES,
    )
    try:
        return await client.execute_dax(query)
    finally:
        await client.close()


# ── Step 3: Parse rows ────────────────────────────────────────────────────────

def parse_rows(
    raw_rows: list[dict[str, Any]], dim: dict[str, str]
) -> list[dict[str, Any]]:
    from ms_renuity_insights_portal.config.models import DimensionRef
    from ms_renuity_insights_portal.engine.kpi_engine import (
        _extract_dim_value,
        _extract_measure_value,
    )

    dim_ref = DimensionRef(
        dimension_name=dim["name"],
        pbi_table_name=dim["pbi_table"],
        pbi_column_name=dim["pbi_column"],
    )
    parsed = []
    for row in raw_rows:
        dim_val = _extract_dim_value(row, dim_ref)
        if not dim_val:
            continue
        kpi_val = _extract_measure_value(row, "KPI Value")
        parsed.append({"dimension_value": dim_val, "kpi_value": kpi_val})
    return parsed


# ── Step 4: Signal detection ─────────────────────────────────────────────────

def detect_signals(
    parsed: list[dict[str, Any]], kpi_format: str
) -> list[dict[str, Any]]:
    breaches = []
    for r in parsed:
        raw = r["kpi_value"]
        if raw is None:
            continue
        display = raw * 100 if kpi_format == "percentage" else raw
        if BREACH_OPERATOR == "lt" and display < BREACH_THRESHOLD:
            breaches.append({
                "dim_value": r["dimension_value"],
                "display_value": round(display, 2),
                "threshold": BREACH_THRESHOLD,
                "delta": round(display - BREACH_THRESHOLD, 2),
                "severity": "critical" if display < BREACH_THRESHOLD * 0.7 else "warning",
            })
    return sorted(breaches, key=lambda x: x["display_value"])


# ── Step 5: WHY analysis — dependency check ──────────────────────────────────

async def run_dependency_check(
    deps: list[dict[str, str]],
    dim: dict[str, str],
    product_dim: dict[str, str] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch each dependency KPI for the same dimension to show WHY breakdown."""
    results: dict[str, list[dict[str, Any]]] = {}
    for dep in deps:
        query = build_query(dep["pbi_measure"], dim, product_dim)
        try:
            raw = await execute_dax(query)
            parsed = parse_rows(raw, dim)
            results[dep["label"]] = parsed
        except Exception as e:
            print(f"    [warn] Dependency '{dep['label']}' failed: {e}")
    return results


# ── Step 6 helper: Feature generation test ───────────────────────────────────

async def _run_feature_test(
    config: dict[str, Any],
    primary_dim: dict[str, str],
    product_dim: dict[str, str] | None,
) -> None:
    """Quick smoke test: fetch current period data and compute rank + mix features."""
    import numpy as np
    from ms_renuity_insights_portal.config.models import DimensionRef
    from ms_renuity_insights_portal.dax.query_builder import DAXQueryBuilder
    from ms_renuity_insights_portal.definitions.features import (
        calculate_rank,
        calculate_rank_pct,
        calculate_region_mix,
    )

    dim_ref = DimensionRef(
        dimension_name=primary_dim["name"],
        pbi_table_name=primary_dim["pbi_table"],
        pbi_column_name=primary_dim["pbi_column"],
    )
    builder = (
        DAXQueryBuilder()
        .with_kpi(config["pbi_measure_name"])
        .group_by(dim_ref)
        .add_date_filter(DATE_TABLE, DATE_COLUMN, TEST_START_DATE, TEST_END_DATE)
    )
    if PRODUCT_FILTER_ENABLED and product_dim:
        prod_ref = DimensionRef(
            dimension_name="Product",
            pbi_table_name=product_dim["pbi_table"],
            pbi_column_name=product_dim["pbi_column"],
        )
        builder = builder.add_member_filter(prod_ref, PRODUCT_FILTER_VALUES)

    raw = await execute_dax(builder.build())
    if not raw:
        print("  [warn] No data for feature test.")
        return

    import pandas as pd
    df = pd.DataFrame(raw)
    kpi_col = None
    dim_col = None
    for c in df.columns:
        if "kpi value" in c.lower():
            kpi_col = c
        if primary_dim["pbi_column"].lower() in c.lower():
            dim_col = c
    if not kpi_col or not dim_col:
        print(f"  [warn] Cannot find KPI/dim column. Columns: {list(df.columns)}")
        return

    df.rename(columns={kpi_col: "KPI Value", dim_col: "dimension"}, inplace=True)
    df["KPI Value"] = pd.to_numeric(df["KPI Value"], errors="coerce")
    df = df[df["dimension"].notna() & (df["dimension"] != "")].reset_index(drop=True)

    ranks = calculate_rank(df, "KPI Value", [])
    rank_pcts = calculate_rank_pct(df, "KPI Value", [])
    mixes = calculate_region_mix(df, "KPI Value", [])

    print(f"\n  {'Dimension':<35} {'Value':>10} {'Rank':>6} {'Rank%':>7} {'Mix%':>7}")
    print(f"  {'-'*35} {'-'*10} {'-'*6} {'-'*7} {'-'*7}")
    for idx in df.index[:15]:
        dim = str(df.at[idx, "dimension"])[:35]
        val = df.at[idx, "KPI Value"]
        rk = ranks.iloc[idx] if not np.isnan(ranks.iloc[idx]) else "-"
        rp = f"{rank_pcts.iloc[idx]:.0f}" if not np.isnan(rank_pcts.iloc[idx]) else "-"
        mx = f"{mixes.iloc[idx]:.1f}" if not np.isnan(mixes.iloc[idx]) else "-"
        val_s = f"{val:.4f}" if val is not None and not np.isnan(val) else "N/A"
        print(f"  {dim:<35} {val_s:>10} {rk!s:>6} {rp!s:>7} {mx!s:>7}")

    print(f"\n  Showing 15 of {len(df)} rows. Features: rank, rank_pct, region_mix")


# ── Single KPI run (steps 0–3; optional 4–6) ─────────────────────────────────

async def run_single_kpi_compute(
    kpi_name: str,
    *,
    skip_signals_and_why: bool,
    print_query: bool = True,
) -> bool:
    """Load config, build DAX, execute, parse. Returns False on fatal config/credential error."""
    _section(f"STEP 0 - KPI: {kpi_name} (load Postgres config)")
    try:
        config = await load_kpi_config(kpi_name)
    except Exception as e:
        print(f"\n  [error] {e}")
        return False

    print(f"  KPI       : {config['kpi_name']} ({config['label']})")
    print(f"  Measure   : {config['pbi_measure_name']}")
    print(f"  Dimensions: {len(config['dimensions'])} valid (with PBI mapping)")
    for d in config["dimensions"]:
        print(f"    - {d['name']:30s}  '{d['pbi_table']}'[{d['pbi_column']}]")
    if not skip_signals_and_why:
        print(f"  Dependencies: {len(config['dependencies'])}")
        for dep in config["dependencies"]:
            print(f"    - {dep['label']:30s}  {dep['pbi_measure']}")
    print(f"  Date range: {TEST_START_DATE} -> {TEST_END_DATE}")

    primary_dim = None
    product_dim = None
    for d in config["dimensions"]:
        if d["name"] == "Market_OpCo":
            primary_dim = d
        if d["name"] == "Product_Lead":
            product_dim = d
    if primary_dim is None and config["dimensions"]:
        primary_dim = config["dimensions"][0]

    if primary_dim is None:
        print("\n  [error] No valid dimensions with PBI mapping found.")
        return False

    print(f"\n  Primary display dimension: {primary_dim['name']}")

    if print_query:
        _section("STEP 1 - Generated DAX Query")
        query = build_query(config["pbi_measure_name"], primary_dim, product_dim)
        print(query)
    else:
        query = build_query(config["pbi_measure_name"], primary_dim, product_dim)

    missing = [n for n, v in {
        "AZURE_TENANT_ID": AZURE_TENANT_ID,
        "AZURE_CLIENT_ID": AZURE_CLIENT_ID,
        "AZURE_CLIENT_SECRET": AZURE_CLIENT_SECRET,
        "PBI_WORKSPACE_ID": PBI_WORKSPACE_ID,
        "PBI_DATASET_ID": PBI_DATASET_ID,
    }.items() if not v or v.startswith("<")]
    if missing:
        print(f"\n  [error] Missing: {', '.join(missing)}")
        print("          Fill .env.dax and retry.")
        return False

    _section("STEP 2 - Executing DAX Query against Power BI")
    t0 = time.monotonic()
    try:
        raw_rows = await execute_dax(query)
        elapsed = int((time.monotonic() - t0) * 1000)
        print(f"  Response: {len(raw_rows)} row(s) in {elapsed} ms")
    except Exception as exc:
        print(f"\n  [error] DAX execution failed: {exc}")
        raise

    if not raw_rows:
        print("\n  [warn] No rows returned.")
        return True

    _section("STEP 3 - Parsed KPI Rows")
    parsed = parse_rows(raw_rows, primary_dim)

    is_pct = "%" in config["label"] or "rate" in config["kpi_name"].lower()
    fmt_suffix = "%" if is_pct else ""

    header = f"{primary_dim['name']:<40} {'Value':>12}"
    print(f"\n  {header}")
    print(f"  {'-'*40} {'-'*12}")
    for r in sorted(parsed, key=lambda x: x["kpi_value"] if x["kpi_value"] is not None else float("inf")):
        if r["kpi_value"] is not None:
            display = r["kpi_value"] * 100 if is_pct else r["kpi_value"]
            val_str = f"{display:.2f}{fmt_suffix}"
        else:
            val_str = "N/A"
        print(f"  {r['dimension_value']:<40} {val_str:>12}")

    values = [
        (r["kpi_value"] * 100 if is_pct else r["kpi_value"])
        for r in parsed if r["kpi_value"] is not None
    ]
    if values:
        print(f"\n  Rows : {len(parsed)}")
        print(f"  Min  : {min(values):.2f}{fmt_suffix}")
        print(f"  Max  : {max(values):.2f}{fmt_suffix}")
        print(f"  Avg  : {sum(values)/len(values):.2f}{fmt_suffix}")

    if not skip_signals_and_why:
        _section(f"STEP 4 - Signal Detection ({config['label']} {BREACH_OPERATOR} {BREACH_THRESHOLD})")
        breaches = detect_signals(parsed, "percentage" if is_pct else "absolute")

        if not breaches:
            print(f"\n  No breaches detected.")
        else:
            print(f"\n  {len(breaches)} breach(es):\n")
            print(f"  {primary_dim['name']:<40} {'Value':>8}  {'Delta':>8}  {'Severity':<10}")
            print(f"  {'-'*40} {'-'*8}  {'-'*8}  {'-'*10}")
            for b in breaches:
                print(
                    f"  {b['dim_value']:<40} {b['display_value']:>7.2f}{fmt_suffix}  "
                    f"{b['delta']:>7.2f}{fmt_suffix}  {b['severity']:<10}"
                )

        if config["dependencies"]:
            _section("STEP 5 - WHY Analysis (Dependency Breakdown)")
            dep_results = await run_dependency_check(
                config["dependencies"], primary_dim, product_dim
            )
            for dep_label, dep_parsed in dep_results.items():
                print(f"\n  >> {dep_label}")
                if not dep_parsed:
                    print("       (no data)")
                    continue
                for r in sorted(dep_parsed, key=lambda x: x["kpi_value"] if x["kpi_value"] is not None else float("inf"))[:5]:
                    val = r["kpi_value"]
                    val_str = f"{val:.4f}" if val is not None else "N/A"
                    print(f"       {r['dimension_value']:<35} {val_str:>12}")

    if os.getenv("TEST_FEATURES", "").lower() in ("1", "true", "yes"):
        _section("STEP 6 - DAX Feature Generation (cross-sectional sample)")
        await _run_feature_test(config, primary_dim, product_dim)

    if os.getenv("DAX_DEBUG", "").lower() in ("1", "true", "yes"):
        _section("RAW DAX RESPONSE (first 5 rows)")
        for r in raw_rows[:5]:
            print("  ", json.dumps(r, default=str, indent=2))

    return True


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("\n" + "=" * 70)
    print("  RENUITY - DAX KPI Compute Test (Config-Driven)")
    print("=" * 70)

    kpis_to_run = TEST_KPIS_BATCH if TEST_KPIS_BATCH else [TEST_KPI]
    skip = SKIP_SIGNALS_AND_WHY or KPI_COMPUTE_BATCH or (len(kpis_to_run) > 1)

    if len(kpis_to_run) > 1:
        print(f"\n  Batch mode: {len(kpis_to_run)} KPI(s), signals/WHY skipped: {skip}")
        for i, kpi in enumerate(kpis_to_run, start=1):
            print("\n" + "#" * 70)
            print(f"  RUN {i}/{len(kpis_to_run)}  —  {kpi}")
            print("#" * 70)
            quiet_query = os.getenv("BATCH_QUIET_DAX", "").lower() in ("1", "true", "yes")
            await run_single_kpi_compute(
                kpi,
                skip_signals_and_why=skip,
                print_query=not quiet_query,
            )
    else:
        await run_single_kpi_compute(
            kpis_to_run[0],
            skip_signals_and_why=skip,
            print_query=True,
        )

    print("\n" + "=" * 70)
    print("  Test complete.")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
