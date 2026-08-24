"""Ad-hoc Power BI data fetch endpoints (multi-KPI, dimensions, filters)."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..dependencies import get_config_loader, get_dax_settings, get_pbi_client
from ...config.config_loader import ConfigLoader
from ...config.models import ConfigIncompleteError, DimensionRef
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
    compare_previous: bool = Field(
        False,
        description=(
            "When true, also fetch the immediately-preceding period of equal "
            "length and append a 'comparison' block (current vs previous, delta, "
            "pct_change) to the response. Requires filters.period to be set."
        ),
    )


class DistinctValuesRequest(BaseModel):
    dimension: str | None = Field(
        None, description="Single dimension name, e.g. region. Use this OR 'dimensions'."
    )
    dimensions: list[str] = Field(
        default_factory=list,
        description="Multiple dimension names to fetch distinct values for in one call.",
    )
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


# Prefix for the internal measure column added when filtering on a *different*
# KPI (e.g. filters = {"own_leads": "> 0"} while fetching pipeline_value).
_HAVING_ALIAS_PREFIX = "__having__"

# Map comparison symbols to the query builder's operator names.
_SYMBOL_OPS = {
    ">=": "gte",
    "<=": "lte",
    "<>": "ne",
    "!=": "ne",
    "==": "eq",
    "=": "eq",
    ">": "gt",
    "<": "lt",
}
_NUM = r"-?\d+(?:\.\d+)?"
_BETWEEN_RE = re.compile(rf"^between\s+({_NUM})\s+and\s+({_NUM})$", re.IGNORECASE)
_CMP_RE = re.compile(rf"^(>=|<=|<>|!=|==|=|>|<)\s*({_NUM})$")
_BARE_RE = re.compile(rf"^({_NUM})$")


def _parse_kpi_condition(raw: Any) -> tuple[str, float, float | None]:
    """Parse a KPI value filter into ``(operator, value, value2)``.

    Accepts a string condition (``"> 0"``, ``">= 100"``, ``"between 1 and 5"``,
    or a bare number meaning equality) or a dict ``{"op":..,"value":..}``.
    """
    if isinstance(raw, dict):
        op = str(raw.get("op", "")).strip().lower()
        op = _SYMBOL_OPS.get(op, op)
        if op not in {"gt", "lt", "gte", "lte", "eq", "ne", "between"}:
            raise HTTPException(400, f"Invalid KPI filter operator: {raw.get('op')!r}")
        val = raw.get("value")
        if val is None:
            raise HTTPException(400, "KPI filter object requires 'value'")
        val2 = raw.get("value2")
        if op == "between" and val2 is None:
            raise HTTPException(400, "KPI filter 'between' requires 'value2'")
        return op, float(val), (float(val2) if val2 is not None else None)

    s = str(raw).strip()
    m = _BETWEEN_RE.match(s)
    if m:
        return "between", float(m.group(1)), float(m.group(2))
    m = _CMP_RE.match(s)
    if m:
        return _SYMBOL_OPS[m.group(1)], float(m.group(2)), None
    m = _BARE_RE.match(s)
    if m:
        return "eq", float(m.group(1)), None
    raise HTTPException(
        400,
        f"Invalid KPI filter condition {raw!r}. "
        "Use e.g. '> 0', '>= 100', '< 500', 'between 1 and 5'.",
    )


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


def _extract_measure_value(row: dict[str, Any], alias: str) -> Any:
    """Return the value of the measure column aliased ``alias`` (e.g. a KPI)."""
    target = alias.strip().lower()
    for k, v in row.items():
        if str(k).strip().strip("[]").lower() == target:
            return v
    return None


def _project_kpi_rows(
    rows: list[dict[str, Any]], dims: list[DimensionRef], alias: str
) -> list[dict[str, Any]]:
    """Extract one KPI's column from the shared multi-measure result.

    Produces the legacy per-KPI shape: dimension columns plus ``[KPI Value]``.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        item: dict[str, Any] = {}
        for d in dims:
            item[d.dimension_name] = _extract_dim_value(r, d)
        item["[KPI Value]"] = _extract_measure_value(r, alias)
        out.append(item)
    return out


def _previous_period(p: PeriodFilter) -> PeriodFilter:
    """The equal-length window immediately preceding ``p`` (inclusive dates)."""
    span = p.end - p.start
    prev_end = p.start - timedelta(days=1)
    prev_start = prev_end - span
    return PeriodFilter(start=prev_start, end=prev_end)


def _aggregate_kpi(rows: list[dict[str, Any]], alias: str) -> float | None:
    """Sum a KPI's value across all rows; None if no numeric value present."""
    total: float | None = None
    for r in rows:
        v = _extract_measure_value(r, alias)
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        total = fv if total is None else total + fv
    return total


def _build_comparison(
    kpi_names: list[str],
    cur_rows: list[dict[str, Any]],
    prev_rows: list[dict[str, Any]],
    cur_p: PeriodFilter,
    prev_p: PeriodFilter,
) -> dict[str, Any]:
    """current vs previous per KPI: value, delta, pct_change, direction."""
    by: dict[str, Any] = {}
    for kpi in kpi_names:
        cur = _aggregate_kpi(cur_rows, kpi)
        prev = _aggregate_kpi(prev_rows, kpi)
        delta: float | None = None
        pct: float | None = None
        direction = "flat"
        if cur is not None and prev is not None:
            delta = cur - prev
            if prev != 0:
                pct = (delta / prev) * 100.0
            direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
        by[kpi] = {
            "current": cur,
            "previous": prev,
            "delta": delta,
            "pct_change": pct,
            "direction": direction,
        }
    return {
        "current_period": {"start": cur_p.start.isoformat(), "end": cur_p.end.isoformat()},
        "previous_period": {"start": prev_p.start.isoformat(), "end": prev_p.end.isoformat()},
        "by_kpi": by,
    }


def _combined_rows(
    rows: list[dict[str, Any]], dims: list[DimensionRef], kpi_names: list[str]
) -> list[dict[str, Any]]:
    """One row per group: dimension values plus one column per requested KPI.

    KPI columns are keyed by KPI name and are ``None`` where the measure is
    blank, so every KPI appears in every row regardless of Power BI omissions.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        item: dict[str, Any] = {}
        for d in dims:
            item[d.dimension_name] = _extract_dim_value(r, d)
        for kpi in kpi_names:
            item[kpi] = _extract_measure_value(r, kpi)
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

    # A filter key is either a mapped dimension (member filter) or a KPI name
    # (value filter on that KPI, e.g. {"own_leads": "> 0"} — the "filter on a
    # different KPI" case). Dimensions take priority when a name is ambiguous.
    member_filters: list[tuple[DimensionRef, list[str]]] = []
    # (kpi_name, pbi_measure_name, op, value, value2)
    kpi_value_filters: list[tuple[str, str, str, float, float | None]] = []
    for k, raw_v in body.filters.items():
        if k == "period":
            continue
        ref = await loader.resolve_dimension_ref(k)
        if ref is not None:
            vals = _parse_member_filter_values(raw_v)
            if vals:
                member_filters.append((ref, vals))
            continue
        # Not a dimension — try to treat the key as a KPI value filter.
        try:
            fcfg = await loader.get_kpi_config(k)
        except ConfigIncompleteError:
            fcfg = None
        if fcfg is not None and fcfg.pbi_measure_name:
            op, val, val2 = _parse_kpi_condition(raw_v)
            kpi_value_filters.append((k, fcfg.pbi_measure_name, op, val, val2))
            continue
        raise HTTPException(
            404,
            f"Filter key '{k}' is neither a mapped dimension nor a known KPI",
        )

    # Resolve every requested KPI's measure up front. All KPIs share the same
    # dimensions/filters, so they go into ONE SUMMARIZECOLUMNS as separate
    # measure columns (aliased by KPI name) — a single Power BI round-trip.
    kpi_measures: list[tuple[str, str]] = []
    for kpi in kpis:
        try:
            kcfg = await loader.get_kpi_config(kpi)
        except ConfigIncompleteError as exc:
            raise HTTPException(404, f"Unknown KPI '{kpi}'") from exc
        if not kcfg.pbi_measure_name:
            raise HTTPException(400, f"KPI '{kpi}' has no pbi_measure_name mapping")
        kpi_measures.append((kpi, kcfg.pbi_measure_name))

    # Base builder without a date filter (dims/members/having are period-agnostic).
    first_name, first_measure = kpi_measures[0]
    base = DAXQueryBuilder().with_kpi(first_measure, alias=first_name).group_by(*dim_refs)
    for name, measure in kpi_measures[1:]:
        base = base.with_extra_measure(name, measure)
    for ref, vals in member_filters:
        base = base.add_member_filter(ref, vals)
    # Filter on other KPI values: add each as a hidden measure, then HAVING.
    for fname, fmeasure, fop, fval, fval2 in kpi_value_filters:
        alias = f"{_HAVING_ALIAS_PREFIX}{fname}"
        base = base.with_extra_measure(alias, fmeasure).add_having(
            fop, fval, fval2, alias=alias
        )
    for h in body.having:
        base = base.add_having(h.op, h.value, h.value2, alias=first_name)

    async def _run_for_period(p: PeriodFilter | None) -> tuple[str, list[dict[str, Any]]]:
        b = base
        if p is not None:
            b = b.add_date_filter(
                settings.DATE_TABLE_NAME, settings.DATE_COLUMN_NAME, p.start, p.end
            )
        q = b.build()
        return q, await pbi.execute_dax(q)

    query, rows = await _run_for_period(period)
    logger.info("[fetch] KPIs=%s generated single DAX query:\n%s", kpis, query)

    by_kpi: dict[str, Any] = {
        kpi: {
            "row_count": len(rows),
            "rows": _project_kpi_rows(rows, dim_refs, kpi),
        }
        for kpi in kpis
    }

    response: dict[str, Any] = {
        "kpis": kpis,
        "dimensions": dims,
        "period": (
            {"start": period.start.isoformat(), "end": period.end.isoformat()}
            if period is not None
            else None
        ),
        "dax_query": query,
        "row_count": len(rows),
        "rows": _combined_rows(rows, dim_refs, kpis),
        "results_by_kpi": by_kpi,
    }

    # Optional: append previous-period comparison without touching the fields above.
    if body.compare_previous:
        if period is None:
            raise HTTPException(
                400, "compare_previous requires filters.period to be set"
            )
        prev_p = _previous_period(period)
        _, prev_rows = await _run_for_period(prev_p)
        response["comparison"] = _build_comparison(kpis, rows, prev_rows, period, prev_p)

    return response


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
    """Return sorted distinct values for one or more dimensions, with optional filters.

    Pass ``dimension`` (single) for the legacy response shape, or ``dimensions``
    (list) to fetch several in one call — returned under ``results_by_dimension``.
    """
    if pbi is None or settings is None:
        raise HTTPException(500, "Power BI settings/client are not configured")

    # Collect requested dimension names from either field, de-duplicated in order.
    requested: list[str] = []
    seen: set[str] = set()
    for name in ([body.dimension] if body.dimension else []) + list(body.dimensions):
        n = (name or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            requested.append(n)
    if not requested:
        raise HTTPException(400, "Provide 'dimension' or 'dimensions'")

    period = _parse_period(body.filters)

    # Member filters (shared across every dimension query).
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

    async def _distinct_for(dim_name: str) -> list[str]:
        dim_ref = await _resolve_dimension_or_400(loader, dim_name)
        builder = DAXQueryBuilder().with_kpi("0", alias="_dummy").group_by(dim_ref)
        if period is not None:
            builder = builder.add_date_filter(
                settings.DATE_TABLE_NAME,
                settings.DATE_COLUMN_NAME,
                period.start,
                period.end,
            )
        for ref, vals in member_filters:
            builder = builder.add_member_filter(ref, vals)
        rows = await pbi.execute_dax(builder.build())
        return sorted(
            {
                v
                for v in (_extract_dim_value(r, dim_ref) for r in rows)
                if v is not None and v != ""
            }
        )

    results = {name: await _distinct_for(name) for name in requested}

    # Legacy single-dimension shape when caller used 'dimension' and only one dim.
    if body.dimension and not body.dimensions and len(requested) == 1:
        only = requested[0]
        return {"dimension": only, "count": len(results[only]), "values": results[only]}

    return {
        "dimensions": requested,
        "results_by_dimension": {
            name: {"count": len(vals), "values": vals} for name, vals in results.items()
        },
    }

