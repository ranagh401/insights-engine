"""Ad-hoc Power BI data fetch endpoints (multi-KPI, dimensions, filters)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..dependencies import get_config_loader, get_dax_settings, get_pbi_client
from ...config.config_loader import ConfigLoader
from ...config.models import DimensionRef
from ...dax.query_builder import DAXQueryBuilder
from ...powerbi.api_client import PBIClient
from ...settings import Settings

router = APIRouter()
logger = logging.getLogger(__name__)


class PeriodFilter(BaseModel):
    start: date
    end: date


_HAVING_OPERATORS = {"gt", "lt", "gte", "lte", "eq", "ne", "between"}


class HavingFilter(BaseModel):
    op: str = Field(
        ...,
        description="Comparison operator: gt | lt | gte | lte | eq | ne | between",
    )
    value: float = Field(..., description="Threshold (low bound for 'between')")
    value2: float | None = Field(
        None, description="High bound, required only when op == 'between'"
    )


class DataFetchRequest(BaseModel):
    kpis: list[str] = Field(..., min_length=1, description="One or more KPI names")
    dimensions: list[str] = Field(
        default_factory=list,
        description=(
            "Zero or more dimension names. Omit (or pass 'SELF') to get the "
            "portfolio-level KPI total with no dimension drill."
        ),
    )
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Optional filters dictionary. "
            "Use key 'period' as {'start':'YYYY-MM-DD','end':'YYYY-MM-DD'}. "
            "Any other key is treated as a dimension filter value/list."
        ),
    )
    having: list[HavingFilter] = Field(
        default_factory=list,
        description=(
            "Optional value filters on the computed KPI (a HAVING clause). "
            "Each condition tests the KPI value being fetched, e.g. "
            "{'op':'gt','value':0}. Multiple conditions are combined with AND."
        ),
    )


class DistinctValuesRequest(BaseModel):
    dimension: str = Field(..., description="Dimension name, e.g. Product_Lead")
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Optional filters dictionary. "
            "Use key 'period' as {'start':'YYYY-MM-DD','end':'YYYY-MM-DD'}. "
            "Any other key is treated as a dimension filter value/list."
        ),
    )


def _parse_period(filters: dict[str, Any]) -> PeriodFilter | None:
    raw = filters.get("period")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HTTPException(400, "filters.period must be an object with start/end")
    try:
        return PeriodFilter.model_validate(raw)
    except Exception as exc:
        raise HTTPException(400, f"Invalid period filter: {exc}") from exc


def _parse_member_filter_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        vals = [str(v).strip() for v in value if str(v).strip()]
        return vals
    if isinstance(value, dict):
        return []
    s = str(value).strip()
    return [s] if s else []


async def _resolve_dimension_or_400(loader: ConfigLoader, dimension_name: str) -> DimensionRef:
    ref = await loader.resolve_dimension_ref(dimension_name)
    if ref is None:
        raise HTTPException(404, f"Dimension '{dimension_name}' not found or not mapped to PBI")
    return ref


def _extract_dim_value(row: dict[str, Any], dim_ref: DimensionRef) -> str | None:
    frag = dim_ref.pbi_column_name.lower()
    for k, v in row.items():
        if frag in str(k).lower():
            if v is None:
                return None
            s = str(v).strip()
            return s if s else None
    return None


def _normalize_rows(rows: list[dict[str, Any]], dims: list[DimensionRef]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        item: dict[str, Any] = {}
        for d in dims:
            item[d.dimension_name] = _extract_dim_value(r, d)
        # Preserve every field from the raw row for transparency/debug.
        for k, v in r.items():
            if k not in item:
                item[str(k)] = v
        out.append(item)
    return out


@router.post(
    "/fetch",
    response_model=dict[str, Any],
    summary="Fetch ad-hoc KPI data from Power BI",
)
async def fetch_data(
    body: DataFetchRequest,
    loader: ConfigLoader = Depends(get_config_loader),
    pbi: PBIClient | None = Depends(get_pbi_client),
    settings: Settings | None = Depends(get_dax_settings),
):
    """Fetch one dataset per KPI for the same dimension/group/filter selection.

    With no dimensions (or the ``SELF`` sentinel) each KPI returns a single
    portfolio-level total row instead of a per-dimension-member breakdown.
    """
    if pbi is None or settings is None:
        raise HTTPException(500, "Power BI settings/client are not configured")
    kpis = [k.strip() for k in body.kpis if k and k.strip()]
    dims = [
        d.strip()
        for d in body.dimensions
        if d and d.strip() and d.strip().upper() != "SELF"
    ]
    if not kpis:
        raise HTTPException(400, "At least one KPI is required")

    for h in body.having:
        if h.op not in _HAVING_OPERATORS:
            raise HTTPException(
                400,
                f"Invalid having operator '{h.op}'. "
                f"Use one of: {', '.join(sorted(_HAVING_OPERATORS))}",
            )
        if h.op == "between" and h.value2 is None:
            raise HTTPException(400, "having op 'between' requires 'value2'")

    period = _parse_period(body.filters)
    dim_refs = [await _resolve_dimension_or_400(loader, d) for d in dims]

    member_filters: list[tuple[DimensionRef, list[str]]] = []
    for k, raw_v in body.filters.items():
        if k == "period":
            continue
        vals = _parse_member_filter_values(raw_v)
        if not vals:
            continue
        ref = await loader.resolve_dimension_ref(k)
        if ref is None:
            raise HTTPException(404, f"Filter dimension '{k}' not found or not mapped to PBI")
        member_filters.append((ref, vals))

    by_kpi: dict[str, Any] = {}
    for kpi in kpis:
        kcfg = await loader.get_kpi_config(kpi)
        if not kcfg.pbi_measure_name:
            raise HTTPException(400, f"KPI '{kpi}' has no pbi_measure_name mapping")

        builder = DAXQueryBuilder().with_kpi(kcfg.pbi_measure_name).group_by(*dim_refs)
        if period is not None:
            builder = builder.add_date_filter(
                settings.DATE_TABLE_NAME,
                settings.DATE_COLUMN_NAME,
                period.start,
                period.end,
            )
        for ref, vals in member_filters:
            builder = builder.add_member_filter(ref, vals)
        for h in body.having:
            builder = builder.add_having(h.op, h.value, h.value2)

        query = builder.build()
        logger.info("[fetch] KPI=%s generated DAX query:\n%s", kpi, query)
        rows = await pbi.execute_dax(query)
        by_kpi[kpi] = {
            "dax_query": query,
            "row_count": len(rows),
            "rows": _normalize_rows(rows, dim_refs),
        }

    return {
        "kpis": kpis,
        "dimensions": dims,
        "period": (
            {"start": period.start.isoformat(), "end": period.end.isoformat()}
            if period is not None
            else None
        ),
        "results_by_kpi": by_kpi,
    }


@router.post(
    "/distinct-values",
    response_model=dict[str, Any],
    summary="List distinct values for a mapped dimension",
)
async def distinct_values(
    body: DistinctValuesRequest,
    loader: ConfigLoader = Depends(get_config_loader),
    pbi: PBIClient | None = Depends(get_pbi_client),
    settings: Settings | None = Depends(get_dax_settings),
):
    """Return sorted distinct values for a dimension, with optional filters."""
    if pbi is None or settings is None:
        raise HTTPException(500, "Power BI settings/client are not configured")
    dim_name = body.dimension.strip()
    if not dim_name:
        raise HTTPException(400, "dimension is required")
    dim_ref = await _resolve_dimension_or_400(loader, dim_name)

    period = _parse_period(body.filters)
    builder = DAXQueryBuilder().with_kpi("0", alias="_dummy").group_by(dim_ref)
    if period is not None:
        builder = builder.add_date_filter(
            settings.DATE_TABLE_NAME,
            settings.DATE_COLUMN_NAME,
            period.start,
            period.end,
        )

    for k, raw_v in body.filters.items():
        if k == "period":
            continue
        vals = _parse_member_filter_values(raw_v)
        if not vals:
            continue
        ref = await loader.resolve_dimension_ref(k)
        if ref is None:
            raise HTTPException(404, f"Filter dimension '{k}' not found or not mapped to PBI")
        builder = builder.add_member_filter(ref, vals)

    query = builder.build()
    rows = await pbi.execute_dax(query)
    values = sorted(
        {
            v
            for v in (_extract_dim_value(r, dim_ref) for r in rows)
            if v is not None and v != ""
        }
    )
    return {
        "dimension": dim_name,
        "count": len(values),
        "values": values,
    }

