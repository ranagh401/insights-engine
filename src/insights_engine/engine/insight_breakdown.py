"""Drill-down KPI breakdown for a selected main insight (portal diagnosis view)."""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ..config.config_loader import ConfigLoader
from ..config.models import DimensionRef
from ..dax.query_builder import DAXQueryBuilder
from ..powerbi.api_client import PBIClient
from ..settings import Settings
from ..store.result_store import ResultStore
from .derived_kpi_breakdown import (
    PORTAL_KPI_BREAKDOWN_DIMENSIONS,
    _DIMENSION_DISPLAY,
    _build_portfolio_breakdown_dax_query,
    _rows_to_value_map,
)
from .derived_kpi_fetcher import (
    _comparison_vs_previous_month,
    _derived_kpi_date_column,
    _load_portal_kpi_configs,
    _mandatory_portfolio_product_lead_filter,
    _shift_period_back_one_month,
)

logger = logging.getLogger(__name__)

_KPI_VALUE_ALIAS = "KPI Value"

MAIN_INSIGHT_BREAKDOWN_DIMENSIONS = PORTAL_KPI_BREAKDOWN_DIMENSIONS


def _normalize_dimension_key(name: str | None) -> str | None:
    if not name or not str(name).strip():
        return None
    return re.sub(r"[\s-]+", "_", str(name).strip().lower())


def _is_kpi_portfolio_parent(parent_dimension: str | None) -> bool:
    key = _normalize_dimension_key(parent_dimension)
    return key in (None, "kpi_portfolio", "kpi", "portfolio")


def breakdown_dimensions_for_parent(parent_dimension: str | None) -> tuple[str, ...]:
    """Portal breakdown dimensions for an insight parent slice.

    KPI Portfolio / rollup insights → all six dimensions (no parent TREATAS).
    Slice insights (e.g. ``stage`` = Discovery) → same list minus the parent dimension.
    """
    if _is_kpi_portfolio_parent(parent_dimension):
        return MAIN_INSIGHT_BREAKDOWN_DIMENSIONS
    parent_key = _normalize_dimension_key(parent_dimension)
    if not parent_key:
        return MAIN_INSIGHT_BREAKDOWN_DIMENSIONS
    return tuple(
        dim
        for dim in MAIN_INSIGHT_BREAKDOWN_DIMENSIONS
        if _normalize_dimension_key(dim) != parent_key
    )


def _parse_insight_kpis(row: dict[str, Any]) -> list[str]:
    raw = (row.get("kpi") or row.get("kpi_family") or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p and p.strip()]


def _parse_row_date(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _build_breakdown_dax_query(
    measure: str,
    settings: Settings,
    *,
    group_dim: DimensionRef,
    parent_dim: DimensionRef,
    parent_value: str,
    period_start: date,
    period_end: date,
) -> str:
    builder = (
        DAXQueryBuilder()
        .with_kpi(measure, alias=_KPI_VALUE_ALIAS)
        .group_by(group_dim)
    )
    mandatory_filters, _ = _mandatory_portfolio_product_lead_filter()
    for dim_ref, member_values in mandatory_filters:
        builder = builder.add_member_filter(dim_ref, member_values)
    return builder.add_member_filter(parent_dim, [parent_value]).add_date_filter(
        settings.DATE_TABLE_NAME,
        _derived_kpi_date_column(settings),
        period_start,
        period_end,
    ).build()


def _filter_insight_kpi_names(
    kpi_names: list[str],
    kpi_name: str | None,
) -> list[str]:
    """When ``kpi_name`` is set, return only that KPI if it belongs to the insight."""
    if not kpi_name or not str(kpi_name).strip():
        return kpi_names
    needle = str(kpi_name).strip().lower()
    matched = [n for n in kpi_names if n.lower() == needle]
    if not matched:
        raise ValueError(
            f"KPI '{kpi_name}' is not on this insight. Insight KPIs: {', '.join(kpi_names)}"
        )
    return matched


async def fetch_insight_breakdown(
    *,
    store: ResultStore,
    loader: ConfigLoader,
    db: AsyncSession,
    pbi: PBIClient,
    settings: Settings,
    insight_id: UUID,
    kpi_name: str | None = None,
) -> dict[str, Any]:
    """Load one main insight and return KPI values by Portal breakdown dimensions."""
    row = await store.get_main_insight_by_id(insight_id, pascal_case=False)
    if not row:
        raise ValueError(f"Main insight '{insight_id}' not found")

    parent_dim_name = (row.get("dimension_name") or "").strip()
    parent_dim_value = (row.get("dimension_value") or "").strip()
    period_label = (row.get("period") or "").strip() or None
    period_start = _parse_row_date(row.get("period_start"))
    period_end = _parse_row_date(row.get("period_end"))

    portfolio_mode = _is_kpi_portfolio_parent(parent_dim_name)

    if not portfolio_mode and not parent_dim_value:
        raise ValueError("Insight is missing dimension_value for a slice-level parent")
    if period_start is None or period_end is None:
        raise ValueError("Insight is missing period_start or period_end")

    kpi_names = _parse_insight_kpis(row)
    if not kpi_names:
        raise ValueError("Insight has no KPI names in kpi / kpi_family")
    kpi_names = _filter_insight_kpi_names(kpi_names, kpi_name)

    child_dims = breakdown_dimensions_for_parent(parent_dim_name)
    if not child_dims:
        raise ValueError(
            f"No breakdown dimensions for parent '{parent_dim_name}'. "
            f"Supported: {', '.join(MAIN_INSIGHT_BREAKDOWN_DIMENSIONS)}"
        )

    parent_ref: DimensionRef | None = None
    if not portfolio_mode:
        parent_ref = await loader.resolve_dimension_ref(parent_dim_name)
        if parent_ref is None:
            raise ValueError(
                f"Parent dimension '{parent_dim_name}' is not mapped in config_dimensionsreunityportal"
            )

    kpi_configs = await _load_portal_kpi_configs(db, tuple(kpi_names))
    prev_start, prev_end = _shift_period_back_one_month(period_start, period_end)
    _, slice_filters_meta = _mandatory_portfolio_product_lead_filter()

    if portfolio_mode:
        parent_filter_meta: dict[str, Any] = {
            "dimension_name": parent_dim_name or "KPI Portfolio",
            "dimension_value": parent_dim_value or None,
            "pbi_expression": None,
            "description": (
                "Portfolio-wide breakdown for the insight period — no parent TREATAS slice filter."
            ),
        }
    else:
        parent_filter_meta = {
            "dimension_name": parent_dim_name,
            "dimension_value": parent_dim_value,
            "pbi_expression": parent_ref.pbi_expression if parent_ref else None,
            "description": (
                "Every breakdown DAX query applies TREATAS on this parent slice "
                "before grouping by the child dimension."
            ),
        }

    breakdowns: list[dict[str, Any]] = []
    for child_dim_name in child_dims:
        child_ref = await loader.resolve_dimension_ref(child_dim_name)
        block: dict[str, Any] = {
            "dimension_name": child_dim_name,
            "dimension_label": _DIMENSION_DISPLAY.get(child_dim_name, child_dim_name),
            "parent_filter": parent_filter_meta,
            "error": None,
            "dax_query_sample": None,
            "values": [],
        }
        if child_ref is None:
            block["error"] = f"Dimension '{child_dim_name}' is not mapped in config_dimensionsreunityportal"
            breakdowns.append(block)
            continue

        members: dict[str, dict[str, Any]] = {}

        for kpi in kpi_names:
            cfg = kpi_configs.get(kpi, {})
            measure = (cfg.get("pbi_measure_name") or cfg.get("measure") or "").strip()
            kpi_format = cfg.get("format") or "number"
            if not measure:
                continue
            try:
                if portfolio_mode:
                    cur_q = _build_portfolio_breakdown_dax_query(
                        measure,
                        settings,
                        group_dim=child_ref,
                        period_start=period_start,
                        period_end=period_end,
                    )
                    prev_q = _build_portfolio_breakdown_dax_query(
                        measure,
                        settings,
                        group_dim=child_ref,
                        period_start=prev_start,
                        period_end=prev_end,
                    )
                else:
                    cur_q = _build_breakdown_dax_query(
                        measure,
                        settings,
                        group_dim=child_ref,
                        parent_dim=parent_ref,
                        parent_value=parent_dim_value,
                        period_start=period_start,
                        period_end=period_end,
                    )
                    prev_q = _build_breakdown_dax_query(
                        measure,
                        settings,
                        group_dim=child_ref,
                        parent_dim=parent_ref,
                        parent_value=parent_dim_value,
                        period_start=prev_start,
                        period_end=prev_end,
                    )
                if block["dax_query_sample"] is None:
                    block["dax_query_sample"] = cur_q
                cur_rows = await pbi.execute_dax(cur_q)
                cur_map = _rows_to_value_map(cur_rows, child_ref)
                prev_rows = await pbi.execute_dax(prev_q)
                prev_map = _rows_to_value_map(prev_rows, child_ref)
            except Exception as exc:
                logger.warning(
                    "Breakdown DAX failed insight=%s child=%s kpi=%s: %s",
                    insight_id,
                    child_dim_name,
                    kpi,
                    exc,
                )
                continue

            for dim_val in sorted(set(cur_map) | set(prev_map)):
                if dim_val not in members:
                    members[dim_val] = {"dimension_value": dim_val, "kpis": {}}
                current = cur_map.get(dim_val)
                previous = prev_map.get(dim_val)
                entry: dict[str, Any] = {"value": current}
                entry.update(
                    _comparison_vs_previous_month(kpi, kpi_format, current, previous)
                )
                members[dim_val]["kpis"][kpi] = entry

        block["values"] = sorted(
            members.values(),
            key=lambda x: str(x.get("dimension_value") or ""),
        )
        breakdowns.append(block)

    return {
        "insight_id": str(insight_id),
        "title": row.get("title"),
        "parent_dimension": parent_dim_name,
        "parent_dimension_value": parent_dim_value,
        "parent_filter": parent_filter_meta,
        "slice_filters": slice_filters_meta,
        "period": period_label,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "previous_month": {
            "period_start": prev_start.isoformat(),
            "period_end": prev_end.isoformat(),
        },
        "kpi_name": kpi_names[0] if len(kpi_names) == 1 else None,
        "kpis": kpi_names,
        "dimensions": list(MAIN_INSIGHT_BREAKDOWN_DIMENSIONS),
        "breakdowns": breakdowns,
    }
