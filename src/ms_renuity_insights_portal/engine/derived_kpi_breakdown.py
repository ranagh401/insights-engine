"""Portfolio-level breakdown of portal KPIs by CRM dimensions."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..config.config_loader import ConfigLoader
from ..config.models import DimensionRef
from ..dax.query_builder import DAXQueryBuilder
from ..powerbi.api_client import PBIClient
from ..settings import Settings
from .derived_kpi_fetcher import (
    PORTAL_DERIVED_KPI_NAMES,
    _derived_kpi_date_column,
    _load_portal_kpi_configs,
    _resolve_portfolio_member_filters,
    resolve_portal_kpi_names,
)
from .kpi_engine import _extract_dim_value, _extract_measure_value

logger = logging.getLogger(__name__)

_KPI_VALUE_ALIAS = "KPI Value"

# Fixed child dimensions for portal KPI tiles and main-insight breakdown (order preserved).
PORTAL_KPI_BREAKDOWN_DIMENSIONS: tuple[str, ...] = (
    "potential",
    "potential_type",
    "opportunity_type",
    "industry_head",
    "stage",
    "sales_rep",
)

_DIMENSION_DISPLAY: dict[str, str] = {
    "potential": "Potential",
    "potential_type": "Potential Type",
    "opportunity_type": "Opportunity Type",
    "industry_head": "Industry Head",
    "stage": "Stage",
    "sales_rep": "Sales Rep",
}


def _rows_to_value_map(
    rows: list[dict[str, Any]], group_dim: DimensionRef
) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        dim_val = _extract_dim_value(row, group_dim).strip()
        if not dim_val:
            continue
        val = _extract_measure_value(row, _KPI_VALUE_ALIAS)
        if val is None:
            continue
        out[dim_val] = float(val)
    return out


def _build_portfolio_breakdown_dax_query(
    measure: str,
    settings: Settings,
    *,
    group_dim: DimensionRef,
    period_start: date,
    period_end: date,
    member_filters: list[tuple[DimensionRef, list[str]]] | None = None,
) -> str:
    builder = (
        DAXQueryBuilder()
        .with_kpi(measure, alias=_KPI_VALUE_ALIAS)
        .group_by(group_dim)
    )
    for dim_ref, member_values in member_filters or []:
        builder = builder.add_member_filter(dim_ref, member_values)
    return builder.add_date_filter(
        settings.DATE_TABLE_NAME,
        _derived_kpi_date_column(settings),
        period_start,
        period_end,
    ).build()


async def fetch_derived_kpi_breakdown(
    loader: ConfigLoader,
    db: AsyncSession,
    pbi: PBIClient,
    settings: Settings,
    *,
    period_start: date,
    period_end: date,
    kpi_names: tuple[str, ...] | None = None,
    slice_filters: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Return each portal KPI broken down by CRM dimensions."""
    allow = await resolve_portal_kpi_names(db, kpi_names)
    if not allow:
        return {
            "count": 0,
            "dimensions": list(PORTAL_KPI_BREAKDOWN_DIMENSIONS),
            "slice_filters": [],
            "period": {
                "start": period_start.isoformat(),
                "end": period_end.isoformat(),
            },
            "kpis": [],
            "message": "No KPIs found in configkpisrenuitycrm.",
        }
    configs = await _load_portal_kpi_configs(db, allow)
    member_filters, slice_filters_meta = await _resolve_portfolio_member_filters(
        loader, slice_filters
    )

    dim_refs: dict[str, DimensionRef | None] = {}
    for dim_name in PORTAL_KPI_BREAKDOWN_DIMENSIONS:
        dim_refs[dim_name] = await loader.resolve_dimension_ref(dim_name)

    kpi_blocks: list[dict[str, Any]] = []

    for name in allow:
        cfg = configs[name]
        measure = (cfg.get("pbi_measure_name") or cfg.get("measure") or "").strip()
        block: dict[str, Any] = {
            "kpi_name": name,
            "label": cfg.get("label") or name,
            "format": cfg.get("format") or "number",
            "description": cfg.get("description"),
            "error": None,
            "breakdowns": [],
        }
        if not cfg.get("config_source"):
            block["error"] = (
                f"KPI '{name}' not found in configkpisrenuitycrm"
            )
            kpi_blocks.append(block)
            continue
        if not measure:
            block["error"] = "missing pbi_measure_name and measure"
            kpi_blocks.append(block)
            continue

        for dim_name in PORTAL_KPI_BREAKDOWN_DIMENSIONS:
            dim_ref = dim_refs[dim_name]
            dim_block: dict[str, Any] = {
                "dimension_name": dim_name,
                "dimension_label": _DIMENSION_DISPLAY.get(dim_name, dim_name),
                "error": None,
                "values": [],
            }
            if dim_ref is None:
                dim_block["error"] = (
                    f"Dimension '{dim_name}' is not mapped in config_dimensionsreunitycrm"
                )
                block["breakdowns"].append(dim_block)
                continue
            try:
                query = _build_portfolio_breakdown_dax_query(
                    measure,
                    settings,
                    group_dim=dim_ref,
                    period_start=period_start,
                    period_end=period_end,
                    member_filters=member_filters,
                )
                rows = await pbi.execute_dax(query)
                value_map = _rows_to_value_map(rows, dim_ref)
                dim_block["values"] = [
                    {"dimension_value": dim_val, "value": val}
                    for dim_val, val in sorted(value_map.items(), key=lambda x: x[0])
                ]
            except Exception as exc:
                dim_block["error"] = str(exc)
                logger.warning(
                    "Portfolio KPI breakdown failed kpi=%s dimension=%s: %s",
                    name,
                    dim_name,
                    exc,
                )
            block["breakdowns"].append(dim_block)

        kpi_blocks.append(block)

    return {
        "count": len(kpi_blocks),
        "dimensions": list(PORTAL_KPI_BREAKDOWN_DIMENSIONS),
        "slice_filters": slice_filters_meta,
        "period": {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
        },
        "kpis": kpi_blocks,
    }


async def fetch_derived_kpi_breakdown_monthly_and_weekly(
    loader: ConfigLoader,
    db: AsyncSession,
    pbi: PBIClient,
    settings: Settings,
    *,
    monthly_start: date,
    monthly_end: date,
    weekly_start: date,
    weekly_end: date,
    monthly_period_label: str | None = None,
    weekly_period_label: str | None = None,
    kpi_names: tuple[str, ...] | None = None,
    slice_filters: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Fetch portfolio KPI breakdowns for monthly and weekly portal windows."""
    monthly = await fetch_derived_kpi_breakdown(
        loader,
        db,
        pbi,
        settings,
        period_start=monthly_start,
        period_end=monthly_end,
        kpi_names=kpi_names,
        slice_filters=slice_filters,
    )
    weekly = await fetch_derived_kpi_breakdown(
        loader,
        db,
        pbi,
        settings,
        period_start=weekly_start,
        period_end=weekly_end,
        kpi_names=kpi_names,
        slice_filters=slice_filters,
    )
    if monthly_period_label:
        monthly["period"]["label"] = monthly_period_label
    if weekly_period_label:
        weekly["period"]["label"] = weekly_period_label
    return {"monthly": monthly, "weekly": weekly}
