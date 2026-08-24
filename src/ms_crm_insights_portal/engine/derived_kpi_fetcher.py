"""Fetch portfolio-level KPI values from Power BI (CRM: configkpisclientcrm only)."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Literal

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config.config_loader import ConfigLoader
from ..config.models import DimensionRef
from ..dax.query_builder import DAXQueryBuilder
from ..models.config_kpi import ConfigKPI
from ..powerbi.api_client import PBIClient
from ..settings import Settings
from .metric_display import (
    kpi_storage_to_percent_point_scale,
    kpi_uses_percentage_point_difference,
)

logger = logging.getLogger(__name__)

_KPI_VALUE_ALIAS = "KPI Value"

ComparisonGranularity = Literal["month", "week"]

# CRM: no fixed Client tile allow-list — load all KPIs from configkpisclientcrm when omitted.
PORTAL_DERIVED_KPI_NAMES: tuple[str, ...] = ()

# Optional portfolio slice filters (TREATAS before evaluating KPI measures).
PORTAL_KPI_SLICE_FILTER_DIMENSIONS: tuple[str, ...] = (
    "sales_rep",
    "stage",
    "lead_source",
)

_SLICE_DIMENSION_DISPLAY: dict[str, str] = {
    "sales_rep": "Sales Rep",
    "stage": "Stage",
    "lead_source": "Lead Source",
}


def parse_portfolio_slice_filter_values(raw: str | None) -> list[str]:
    """Split a query param into member values (comma-separated, trimmed, deduped)."""
    if raw is None or not str(raw).strip():
        return []
    seen: set[str] = set()
    values: list[str] = []
    for part in str(raw).split(","):
        value = part.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def _derived_kpi_date_column(settings: Settings) -> str:
    """Calendar column for period filters on portfolio-level KPI DAX."""
    return settings.DATE_COLUMN_NAME.strip()


async def resolve_portal_kpi_names(
    db: AsyncSession,
    kpi_names: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """KPI allow-list for portal tiles: explicit names, else all configkpisclientcrm rows."""
    if kpi_names:
        return tuple(kpi_names)
    if PORTAL_DERIVED_KPI_NAMES:
        return PORTAL_DERIVED_KPI_NAMES
    return await _default_portal_kpi_names(db)


async def _default_portal_kpi_names(db: AsyncSession) -> tuple[str, ...]:
    """All configured base KPIs (CRM uses configkpisclientcrm only)."""
    result = await db.execute(select(ConfigKPI.kpi_name).order_by(ConfigKPI.kpi_name))
    return tuple(row[0] for row in result.all() if row[0])


def _extract_scalar_kpi_value(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    alias_lower = _KPI_VALUE_ALIAS.lower()
    for row in rows:
        for k, v in row.items():
            if alias_lower in str(k).lower():
                if v is None:
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
    row = rows[0]
    for v in row.values():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


async def _load_portal_kpi_configs(
    db: AsyncSession,
    kpi_names: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Resolve measure metadata from ``configkpisclientcrm``."""
    if not kpi_names:
        return {}

    base_result = await db.execute(
        select(ConfigKPI).where(ConfigKPI.kpi_name.in_(kpi_names))
    )
    base = {r.kpi_name: r for r in base_result.scalars().all()}

    out: dict[str, dict[str, Any]] = {}
    for name in kpi_names:
        row = base.get(name)
        if row is None:
            out[name] = {
                "kpi_name": name,
                "label": name,
                "format": "number",
                "description": None,
                "pbi_measure_name": None,
                "measure": None,
                "config_source": None,
            }
            continue
        out[name] = {
            "kpi_name": name,
            "label": row.label,
            "format": row.format,
            "description": getattr(row, "description", None),
            "pbi_measure_name": row.pbi_measure_name,
            "measure": row.measure,
            "config_source": "configkpisclientcrm",
        }
    return out


def _shift_period_back_one_month(period_start: date, period_end: date) -> tuple[date, date]:
    """Same calendar window one month earlier (e.g. 15–21 Mar → 15–21 Feb)."""
    return (
        period_start - relativedelta(months=1),
        period_end - relativedelta(months=1),
    )


def _shift_period_back_one_week(period_start: date, period_end: date) -> tuple[date, date]:
    """Prior window of equal length ending the day before ``period_start``."""
    span_days = (period_end - period_start).days + 1
    shift = timedelta(days=span_days)
    return period_start - shift, period_end - shift


def _shift_prior_period(
    period_start: date,
    period_end: date,
    granularity: ComparisonGranularity,
) -> tuple[date, date]:
    if granularity == "week":
        return _shift_period_back_one_week(period_start, period_end)
    return _shift_period_back_one_month(period_start, period_end)


def _prior_period_label(granularity: ComparisonGranularity) -> str:
    return "previous week" if granularity == "week" else "previous month"


async def _resolve_portfolio_slice_filters(
    loader: ConfigLoader,
    slice_filters: dict[str, list[str]] | None,
) -> tuple[list[tuple[DimensionRef, list[str]]], list[dict[str, Any]]]:
    """Resolve optional sales_rep / stage / lead_source query filters for portfolio DAX."""
    if not slice_filters:
        return [], []

    member_filters: list[tuple[DimensionRef, list[str]]] = []
    meta: list[dict[str, Any]] = []
    for dim_name in PORTAL_KPI_SLICE_FILTER_DIMENSIONS:
        raw_vals = slice_filters.get(dim_name)
        if not raw_vals:
            continue
        values = [str(v).strip() for v in raw_vals if str(v).strip()]
        if not values:
            continue
        dim_ref = await loader.resolve_dimension_ref(dim_name)
        if dim_ref is None:
            raise ValueError(
                f"Slice dimension '{dim_name}' is not mapped in config_dimensionsreunitycrm"
            )
        member_filters.append((dim_ref, values))
        meta.append(
            {
                "dimension_name": dim_name,
                "dimension_label": _SLICE_DIMENSION_DISPLAY.get(dim_name, dim_name),
                "dimension_values": values,
                "dimension_value": ", ".join(values),
                "pbi_expression": dim_ref.pbi_expression,
                "mandatory": False,
            }
        )
    return member_filters, meta


def _mandatory_portfolio_product_lead_filter() -> tuple[
    list[tuple[DimensionRef, list[str]]], list[dict[str, Any]]
]:
    """CRM fork: no hard-coded Client Product_Lead=Bath portfolio filter."""
    return [], []


async def _resolve_portfolio_member_filters(
    loader: ConfigLoader,
    slice_filters: dict[str, list[str]] | None,
) -> tuple[list[tuple[DimensionRef, list[str]]], list[dict[str, Any]]]:
    """Optional portfolio slice filters (query params only)."""
    return await _resolve_portfolio_slice_filters(loader, slice_filters)


def _build_portal_kpi_dax_query(
    measure: str,
    settings: Settings,
    period_start: date | None,
    period_end: date | None,
    member_filters: list[tuple[DimensionRef, list[str]]] | None = None,
) -> str:
    builder = DAXQueryBuilder().with_kpi(measure)
    for dim_ref, member_values in member_filters or []:
        builder = builder.add_member_filter(dim_ref, member_values)
    if period_start is not None and period_end is not None:
        builder = builder.add_date_filter(
            settings.DATE_TABLE_NAME,
            _derived_kpi_date_column(settings),
            period_start,
            period_end,
        )
    return builder.build()


def _comparison_vs_previous_month(
    kpi_name: str,
    kpi_format: str,
    current: float | None,
    previous: float | None,
    *,
    granularity: ComparisonGranularity = "month",
) -> dict[str, Any]:
    """Build change metrics and a short line for the portal tile."""
    prior_label = _prior_period_label(granularity)
    change_field = (
        "change_from_previous_week"
        if granularity == "week"
        else "change_from_previous_month"
    )
    out: dict[str, Any] = {
        "previous_value": previous,
        "change_from_previous_month": None,
        "change_from_previous_week": None,
        "change_unit": None,
        "comparison_text": None,
    }
    if current is None or previous is None:
        if previous is None and current is not None:
            out["comparison_text"] = f"No {prior_label} value available"
        return out

    use_pp = kpi_uses_percentage_point_difference(kpi_name)
    is_pct = (kpi_format or "").strip().lower() == "percentage"

    if use_pp:
        cur_s = kpi_storage_to_percent_point_scale(current)
        prev_s = kpi_storage_to_percent_point_scale(previous)
        delta = round(cur_s - prev_s, 2)
        out[change_field] = delta
        out["change_unit"] = "percentage_points"
        mag = abs(delta)
        if mag < 0.05:
            out["comparison_text"] = f"Unchanged from {prior_label}"
        elif delta > 0:
            out["comparison_text"] = f"{mag:g} pp increase from {prior_label}"
        else:
            out["comparison_text"] = f"{mag:g} pp decrease from {prior_label}"
        return out

    if is_pct:
        cur_s = kpi_storage_to_percent_point_scale(current)
        prev_s = kpi_storage_to_percent_point_scale(previous)
        if prev_s == 0:
            out["comparison_text"] = f"{prior_label.capitalize()} value was zero"
            return out
        pct = round(((cur_s - prev_s) / abs(prev_s)) * 100, 1)
    elif previous == 0:
        out["comparison_text"] = f"{prior_label.capitalize()} value was zero"
        return out
    else:
        pct = round(((current - previous) / abs(previous)) * 100, 1)

    out[change_field] = pct
    out["change_unit"] = "percent"
    mag = abs(pct)
    if mag < 0.05:
        out["comparison_text"] = f"Unchanged from {prior_label}"
    elif pct > 0:
        out["comparison_text"] = f"{mag:g}% increase from {prior_label}"
    else:
        out["comparison_text"] = f"{mag:g}% decrease from {prior_label}"
    return out


async def fetch_all_derived_kpi_values(
    loader: ConfigLoader,
    db: AsyncSession,
    pbi: PBIClient,
    settings: Settings,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
    kpi_names: tuple[str, ...] | None = None,
    slice_filters: dict[str, list[str]] | None = None,
    comparison_granularity: ComparisonGranularity = "month",
) -> dict[str, Any]:
    """Evaluate portfolio KPIs in PBI (default: all rows in configkpisclientcrm)."""
    allow = await resolve_portal_kpi_names(db, kpi_names)
    if not allow:
        return {
            "kpis": [],
            "period": None,
            "slice_filters": [],
            "message": "No KPIs found in configkpisclientcrm.",
        }
    configs = await _load_portal_kpi_configs(db, allow)
    member_filters, slice_filters_meta = await _resolve_portfolio_member_filters(
        loader, slice_filters
    )

    meta: list[dict[str, Any]] = []

    for name in allow:
        cfg = configs[name]
        measure = (cfg.get("pbi_measure_name") or cfg.get("measure") or "").strip()
        entry: dict[str, Any] = {
            "kpi_name": name,
            "label": cfg.get("label") or name,
            "format": cfg.get("format") or "number",
            "description": cfg.get("description"),
            "pbi_measure_name": cfg.get("pbi_measure_name"),
            "config_source": cfg.get("config_source"),
            "value": None,
            "previous_value": None,
            "change_from_previous_month": None,
            "change_from_previous_week": None,
            "change_unit": None,
            "comparison_text": None,
            "error": None,
            "dax_query": None,
            "dax_query_previous_month": None,
            "dax_query_previous_week": None,
        }
        if not cfg.get("config_source"):
            entry["error"] = (
                f"KPI '{name}' not found in configkpisclientcrm"
            )
            meta.append(entry)
            continue
        if not measure:
            entry["error"] = "missing pbi_measure_name and measure"
            meta.append(entry)
            continue
        entry["dax_query"] = _build_portal_kpi_dax_query(
            measure,
            settings,
            period_start,
            period_end,
            member_filters=member_filters,
        )
        if period_start is not None and period_end is not None:
            prev_start, prev_end = _shift_prior_period(
                period_start, period_end, comparison_granularity
            )
            prior_dax = _build_portal_kpi_dax_query(
                measure,
                settings,
                prev_start,
                prev_end,
                member_filters=member_filters,
            )
            if comparison_granularity == "week":
                entry["dax_query_previous_week"] = prior_dax
            else:
                entry["dax_query_previous_month"] = prior_dax
        meta.append(entry)

    to_run = [e for e in meta if e.get("dax_query") and not e.get("error")]
    if to_run:
        logger.info(
            "Fetching %d portal KPI value(s) from Power BI (current + prior %s)",
            len(to_run),
            comparison_granularity,
        )
        for entry in to_run:
            kpi_name = entry["kpi_name"]
            kpi_format = entry.get("format") or "number"
            try:
                rows = await pbi.execute_dax(entry["dax_query"])
                entry["value"] = _extract_scalar_kpi_value(rows)
            except Exception as exc:
                entry["error"] = str(exc)
                logger.warning("Derived KPI '%s' current DAX failed: %s", kpi_name, exc)
                continue
            prev_q = entry.get("dax_query_previous_week") or entry.get(
                "dax_query_previous_month"
            )
            if not prev_q:
                continue
            try:
                prev_rows = await pbi.execute_dax(prev_q)
                prev_val = _extract_scalar_kpi_value(prev_rows)
            except Exception as exc:
                logger.warning(
                    "Derived KPI '%s' prior-%s DAX failed: %s",
                    kpi_name,
                    comparison_granularity,
                    exc,
                )
                entry.update(
                    _comparison_vs_previous_month(
                        kpi_name,
                        kpi_format,
                        entry.get("value"),
                        None,
                        granularity=comparison_granularity,
                    )
                )
                continue
            entry.update(
                _comparison_vs_previous_month(
                    kpi_name,
                    kpi_format,
                    entry.get("value"),
                    prev_val,
                    granularity=comparison_granularity,
                )
            )

    period_payload: dict[str, Any] | None = None
    if period_start is not None and period_end is not None:
        prev_start, prev_end = _shift_prior_period(
            period_start, period_end, comparison_granularity
        )
        prior_window = {
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
        }
        period_payload = {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
            "comparison_granularity": comparison_granularity,
        }
        if comparison_granularity == "week":
            period_payload["previous_week"] = prior_window
        else:
            period_payload["previous_month"] = prior_window

    return {
        "count": len(meta),
        "period": period_payload,
        "slice_filters": slice_filters_meta,
        "kpis": meta,
    }


async def fetch_derived_kpis_monthly_and_weekly(
    loader: ConfigLoader,
    db: AsyncSession,
    pbi: PBIClient,
    settings: Settings,
    *,
    monthly_start: date | None,
    monthly_end: date | None,
    weekly_start: date | None,
    weekly_end: date | None,
    monthly_period_label: str | None = None,
    weekly_period_label: str | None = None,
    slice_filters: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Fetch all derived KPIs twice — once per monthly window, once per weekly."""
    monthly = await fetch_all_derived_kpi_values(
        loader,
        db,
        pbi,
        settings,
        period_start=monthly_start,
        period_end=monthly_end,
        slice_filters=slice_filters,
        comparison_granularity="month",
    )
    weekly = await fetch_all_derived_kpi_values(
        loader,
        db,
        pbi,
        settings,
        period_start=weekly_start,
        period_end=weekly_end,
        slice_filters=slice_filters,
        comparison_granularity="week",
    )
    if monthly_period_label and monthly.get("period"):
        monthly["period"]["label"] = monthly_period_label
    if weekly_period_label and weekly.get("period"):
        weekly["period"]["label"] = weekly_period_label
    return {
        "slice_filters": monthly.get("slice_filters") or [],
        "monthly": monthly,
        "weekly": weekly,
    }
