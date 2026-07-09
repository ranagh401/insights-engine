"""Portal-facing APIs: derived KPI tiles and executive insight summaries."""

from __future__ import annotations

import logging
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any, Optional

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.session import get_db
from ...engine.derived_kpi_breakdown import (
    fetch_derived_kpi_breakdown,
    fetch_derived_kpi_breakdown_monthly_and_weekly,
)
from ...engine.derived_kpi_fetcher import (
    ComparisonGranularity,
    fetch_all_derived_kpi_values,
    fetch_derived_kpis_monthly_and_weekly,
    parse_portfolio_slice_filter_values,
)
from ...config.narrative_llm import MainInsightsNarrativeModel
from ...engine.insight_engine import InsightEngine
from ...engine.portal_period import (
    PORTAL_MONTHLY_PERIOD_LABEL,
    PORTAL_WEEKLY_PERIOD_LABEL,
    PortalPeriodType,
    parse_period_window_dates,
    period_window_from_rows,
    resolve_portal_period_window,
    split_portal_insight_rows,
)
from ...engine.insight_breakdown import fetch_insight_breakdown
from ...store.result_store import MAIN_INSIGHTS_TABLE_QUERY_HELP, ResultStore
from ..dependencies import get_config_loader, get_dax_settings, get_pbi_client, get_result_store

logger = logging.getLogger(__name__)
router = APIRouter()


class PeriodTypeOption(str, Enum):
    """Swagger-visible enum for monthly vs weekly portal windows."""

    monthly = "monthly"
    weekly = "weekly"


class PortalKpiNameOption(str, Enum):
    """Legacy Swagger examples — any ``kpiname`` from configkpisrenuitycrm is accepted."""

    net_close_rate = "net_close_rate"
    demo_rate = "demo_rate"
    gross_close_rate = "gross_close_rate"
    avg_ticket_size = "avg_ticket_size"
    cost_per_lead = "cost_per_lead"
    cost_per_demo = "cost_per_demo"
    issue_rate = "issue_rate"
    set_rate = "set_rate"
    commit_slipped_count = "commit_slipped_count"
    oem_leads = "oem_leads"


KpiNameQuery = Annotated[
    str,
    Query(
        title="KPI name",
        description="KPI ``kpiname`` from insights.configkpisrenuitycrm.",
        examples=["commit_slipped_count", "oem_leads", "demo_rate"],
    ),
]

InsightKpiNameQuery = Annotated[
    Optional[str],
    Query(
        title="KPI name",
        description=(
            "Optional: return breakdown for one KPI only (fewer Power BI queries). "
            "Must be a ``kpiname`` in insights.configkpisrenuitycrm. "
            "Omit to fetch all KPIs on the insight."
        ),
    ),
]

PeriodTypeQuery = Annotated[
    Optional[PeriodTypeOption],
    Query(
        title="Period type",
        description=(
            "Choose which reporting window to use.\n\n"
            "- **monthly** — `1-30 Apr` from main_insights, or error if unavailable\n"
            "- **weekly** — main_insights `22-28 Jun`, else default **22-28 Jun 2026**\n"
            "- *omit* — return both (requires main_insights)"
        ),
    ),
]


def _to_portal_period(opt: PeriodTypeOption | None) -> PortalPeriodType | None:
    if opt is None:
        return None
    return PortalPeriodType(opt.value)


def _comparison_granularity_for_period_type(
    period_type: PeriodTypeOption | None,
) -> ComparisonGranularity:
    if period_type == PeriodTypeOption.weekly:
        return "week"
    return "month"


def _select_main_insights_store(store: ResultStore, table_name: Optional[str]) -> ResultStore:
    try:
        return store.with_main_insights_table(table_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


async def _resolve_insight_period_windows(
    store: ResultStore,
    run_timestamp: Optional[datetime],
) -> tuple[Optional[datetime], dict[str, Any] | None, dict[str, Any] | None]:
    """Return (run_timestamp, monthly_window, weekly_window) for fixed portal periods."""
    ts_used = run_timestamp or await store.get_latest_main_insight_run_timestamp()
    if not ts_used:
        return None, None, None
    rows = await store.list_main_insight_rows(
        run_timestamp=ts_used,
        limit=5000,
        pascal_case=False,
    )
    monthly_rows, weekly_rows = split_portal_insight_rows(rows)
    return (
        ts_used,
        period_window_from_rows(monthly_rows, period_label=PORTAL_MONTHLY_PERIOD_LABEL),
        period_window_from_rows(weekly_rows, period_label=PORTAL_WEEKLY_PERIOD_LABEL),
    )


def _window_for_bucket(
    bucket: PortalPeriodType,
    monthly_win: dict[str, Any] | None,
    weekly_win: dict[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    try:
        return resolve_portal_period_window(bucket, monthly_win, weekly_win)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


def _flatten_kpi_breakdown_payload(
    raw: dict[str, Any],
    *,
    kpi_name: str,
) -> dict[str, Any]:
    """Single-KPI response: breakdowns at top level (no nested ``kpis`` array)."""
    kpi = (raw.get("kpis") or [{}])[0]
    return {
        "kpi_name": kpi.get("kpi_name") or kpi_name,
        "label": kpi.get("label"),
        "format": kpi.get("format"),
        "description": kpi.get("description"),
        "error": kpi.get("error"),
        "dimensions": raw.get("dimensions"),
        "slice_filters": raw.get("slice_filters") or [],
        "period": raw.get("period"),
        "breakdowns": kpi.get("breakdowns") or [],
    }


def _portfolio_slice_filters_from_query(
    sales_rep: str | None,
    stage: str | None,
    lead_source: str | None,
) -> dict[str, list[str]] | None:
    """Build optional sales_rep / stage / lead_source slice for portfolio DAX."""
    filters: dict[str, list[str]] = {}
    sales_rep_vals = parse_portfolio_slice_filter_values(sales_rep)
    stage_vals = parse_portfolio_slice_filter_values(stage)
    lead_source_vals = parse_portfolio_slice_filter_values(lead_source)
    if sales_rep_vals:
        filters["sales_rep"] = sales_rep_vals
    if stage_vals:
        filters["stage"] = stage_vals
    if lead_source_vals:
        filters["lead_source"] = lead_source_vals
    return filters or None


def _parse_explicit_period(
    period_start: date | None,
    period_end: date | None,
) -> tuple[date, date] | None:
    """When both dates are set, use them directly for PBI (no main_insights lookup)."""
    if period_start is None and period_end is None:
        return None
    if period_start is None or period_end is None:
        raise HTTPException(
            status_code=400,
            detail="Provide both period_start and period_end (YYYY-MM-DD), or omit both.",
        )
    if period_end < period_start:
        raise HTTPException(
            status_code=400,
            detail="period_end must be on or after period_start.",
        )
    return period_start, period_end


class ExecutiveSummaryBucket(BaseModel):
    period: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    insight_count: int = 0
    pointers: list[str] = Field(
        default_factory=list,
        description="Five LLM-generated bullets (max 10 words each) from maininsightscrm rows",
    )


class ExecutiveSummaryResponse(BaseModel):
    run_timestamp: Optional[datetime] = None
    period_type: Optional[str] = Field(
        None,
        description="Echo of request: monthly, weekly, or null when both were returned.",
    )
    monthly: Optional[ExecutiveSummaryBucket] = None
    weekly: Optional[ExecutiveSummaryBucket] = None


@router.get(
    "/derived-kpis",
    response_model=dict[str, Any],
    summary="Fetch portal KPI tiles from configkpisrenuitycrm",
    description=(
        "Portfolio-level KPI tiles with prior-period comparison. "
        "KPI list and PBI measures come from **insights.configkpisrenuitycrm** "
        "(CRM has no separate derived-KPI table).\n\n"
        "Optional **slice filters** (combine as needed; applied via TREATAS before evaluating measures):\n"
        "- `sales_rep` — e.g. `Anurag Kapoor`\n"
        "- `stage` — e.g. `Discovery`\n"
        "- `lead_source` — comma-separated for multiple, e.g. `Sales - Organic,Inbound`\n\n"
        "**Date window** (pick one):\n"
        "- `period_start` + `period_end` (YYYY-MM-DD) — query Power BI directly; "
        "**no main_insights required**\n"
        "- or omit dates and resolve from latest `main_insights` run (`run_timestamp` optional):\n"
        "  - `monthly` → `1-30 Apr`\n"
        "  - `weekly` → main_insights `22-28 Jun`, else default **2026-06-22 → 2026-06-28**\n"
        "  - omit `period_type` → both (requires main_insights)"
    ),
)
async def get_derived_kpis(
    period_type: PeriodTypeQuery = None,
    period_start: Optional[date] = Query(
        None,
        description="Period start (YYYY-MM-DD). Use with period_end to query PBI without main_insights.",
    ),
    period_end: Optional[date] = Query(
        None,
        description="Period end (YYYY-MM-DD). Use with period_start to query PBI without main_insights.",
    ),
    sales_rep: Optional[str] = Query(
        None,
        description="Optional Sales Rep member value(s) to filter the portfolio slice before KPI evaluation.",
    ),
    stage: Optional[str] = Query(
        None,
        description="Optional Stage member value(s) to filter the portfolio slice before KPI evaluation.",
    ),
    lead_source: Optional[str] = Query(
        None,
        description=(
            "Optional Lead Source member value(s). Comma-separated for multiple, "
            "e.g. `Sales - Organic,Inbound`."
        ),
    ),
    run_timestamp: Optional[datetime] = Query(
        None,
        description=(
            "Use main_insights from this run to resolve date windows. "
            "If omitted, uses the latest run_timestamp in main_insights."
        ),
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store: ResultStore = Depends(get_result_store),
    loader=Depends(get_config_loader),
    db: AsyncSession = Depends(get_db),
    pbi=Depends(get_pbi_client),
    settings=Depends(get_dax_settings),
):
    """Portal KPI tiles with prior-period comparison (week-over-week or month-over-month)."""
    if pbi is None or settings is None:
        raise HTTPException(500, "Power BI settings/client are not configured")

    slice_filters = _portfolio_slice_filters_from_query(sales_rep, stage, lead_source)
    explicit_period = _parse_explicit_period(period_start, period_end)

    try:
        if explicit_period is not None:
            start, end = explicit_period
            logger.info(
                "GET /pbi/portal/derived-kpis explicit %s→%s slice_filters=%s",
                start,
                end,
                slice_filters,
            )
            data = await fetch_all_derived_kpi_values(
                loader,
                db,
                pbi,
                settings,
                period_start=start,
                period_end=end,
                slice_filters=slice_filters,
                comparison_granularity=_comparison_granularity_for_period_type(
                    period_type
                ),
            )
            data["period_source"] = "query"
            if period_type is not None:
                data["period_type"] = period_type.value
            return data

        store = _select_main_insights_store(store, main_insights_table)
        ts_used, m_win, w_win = await _resolve_insight_period_windows(store, run_timestamp)

        if period_type is not None:
            bucket = _to_portal_period(period_type)
            assert bucket is not None
            win, period_source = _window_for_bucket(bucket, m_win, w_win)
            start, end = parse_period_window_dates(win)
            logger.info(
                "GET /pbi/portal/derived-kpis period_type=%s %s→%s slice_filters=%s "
                "run_timestamp=%s period_source=%s",
                period_type.value,
                start,
                end,
                slice_filters,
                ts_used,
                period_source,
            )
            data = await fetch_all_derived_kpi_values(
                loader,
                db,
                pbi,
                settings,
                period_start=start,
                period_end=end,
                slice_filters=slice_filters,
                comparison_granularity=_comparison_granularity_for_period_type(
                    period_type
                ),
            )
            if data.get("period"):
                data["period"]["label"] = win.get("period")
            data["period_source"] = period_source
            if period_source == "main_insights":
                data["run_timestamp"] = ts_used
            data["period_type"] = period_type.value
            return data

        if not ts_used:
            raise HTTPException(404, "No main_insights run found to resolve period dates.")

        monthly_start, monthly_end = parse_period_window_dates(m_win)
        weekly_start, weekly_end = parse_period_window_dates(w_win)
        if monthly_start is None or monthly_end is None:
            raise HTTPException(
                404,
                f"Could not resolve monthly period '{PORTAL_MONTHLY_PERIOD_LABEL}' in main_insights.",
            )
        if weekly_start is None or weekly_end is None:
            raise HTTPException(
                404,
                f"Could not resolve weekly period '{PORTAL_WEEKLY_PERIOD_LABEL}' in main_insights.",
            )

        logger.info(
            "GET /pbi/portal/derived-kpis monthly=%s→%s weekly=%s→%s slice_filters=%s run_timestamp=%s",
            monthly_start,
            monthly_end,
            weekly_start,
            weekly_end,
            slice_filters,
            ts_used,
        )
        payload = await fetch_derived_kpis_monthly_and_weekly(
            loader,
            db,
            pbi,
            settings,
            monthly_start=monthly_start,
            monthly_end=monthly_end,
            weekly_start=weekly_start,
            weekly_end=weekly_end,
            monthly_period_label=PORTAL_MONTHLY_PERIOD_LABEL,
            weekly_period_label=PORTAL_WEEKLY_PERIOD_LABEL,
            slice_filters=slice_filters,
        )
        payload["run_timestamp"] = ts_used
        return payload
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/derived-kpis/breakdown",
    response_model=dict[str, Any],
    summary="Portal KPI breakdown by Potential, Potential Type, Opportunity Type, Industry Head, Stage, and Sales Rep",
    description=(
        "Break down **one** portal KPI **dimension value-wise** for "
        "**Potential**, **Potential Type**, **Opportunity Type**, **Industry Head**, "
        "**Stage**, and **Sales Rep** (Power BI only — no main_insights table parameter).\n\n"
        "Returns current-period values only (no prior-month comparison).\n\n"
        "Optional **slice filters** (combine as needed; applied via TREATAS before grouping):\n"
        "- `sales_rep` — e.g. `Anurag Kapoor`\n"
        "- `stage` — e.g. `Discovery`\n"
        "- `lead_source` — comma-separated for multiple, e.g. `Sales - Organic,Inbound`\n\n"
        "**Date window** (pick one):\n"
        "- `period_start` + `period_end` (YYYY-MM-DD) — query Power BI directly; "
        "**no main_insights required**\n"
        "- or omit dates and resolve from latest `main_insights` run (`run_timestamp` optional):\n"
        "  - `period_type=weekly` → main_insights if available, else default **2026-06-22 → 2026-06-28**"
    ),
)
async def get_derived_kpis_breakdown(
    kpi_name: KpiNameQuery,
    period_type: PeriodTypeQuery = None,
    period_start: Optional[date] = Query(
        None,
        description="Period start (YYYY-MM-DD). Use with period_end to query PBI without main_insights.",
    ),
    period_end: Optional[date] = Query(
        None,
        description="Period end (YYYY-MM-DD). Use with period_start to query PBI without main_insights.",
    ),
    sales_rep: Optional[str] = Query(
        None,
        description="Optional Sales Rep member value(s) to filter the portfolio slice before breakdown.",
    ),
    stage: Optional[str] = Query(
        None,
        description="Optional Stage member value(s) to filter the portfolio slice before breakdown.",
    ),
    lead_source: Optional[str] = Query(
        None,
        description=(
            "Optional Lead Source member value(s). Comma-separated for multiple, "
            "e.g. `Sales - Organic,Inbound`."
        ),
    ),
    run_timestamp: Optional[datetime] = Query(
        None,
        description=(
            "Use main_insights from this run to resolve date windows only. "
            "If omitted, uses the latest run_timestamp in main_insights."
        ),
    ),
    store: ResultStore = Depends(get_result_store),
    loader=Depends(get_config_loader),
    db: AsyncSession = Depends(get_db),
    pbi=Depends(get_pbi_client),
    settings=Depends(get_dax_settings),
):
    """Portfolio drill-down for one KPI × CRM breakdown dimensions."""
    if pbi is None or settings is None:
        raise HTTPException(500, "Power BI settings/client are not configured")

    kpi_tuple = (kpi_name,)
    slice_filters = _portfolio_slice_filters_from_query(sales_rep, stage, lead_source)
    explicit_period = _parse_explicit_period(period_start, period_end)

    try:
        if explicit_period is not None:
            start, end = explicit_period
            logger.info(
                "GET /pbi/portal/derived-kpis/breakdown kpi=%s explicit %s→%s slice_filters=%s",
                kpi_name,
                start,
                end,
                slice_filters,
            )
            raw = await fetch_derived_kpi_breakdown(
                loader,
                db,
                pbi,
                settings,
                period_start=start,
                period_end=end,
                kpi_names=kpi_tuple,
                slice_filters=slice_filters,
            )
            data = _flatten_kpi_breakdown_payload(raw, kpi_name=kpi_name)
            data["period_source"] = "query"
            if period_type is not None:
                data["period_type"] = period_type.value
            return data

        ts_used, m_win, w_win = await _resolve_insight_period_windows(store, run_timestamp)

        if period_type is not None:
            bucket = _to_portal_period(period_type)
            assert bucket is not None
            win, period_source = _window_for_bucket(bucket, m_win, w_win)
            start, end = parse_period_window_dates(win)
            logger.info(
                "GET /pbi/portal/derived-kpis/breakdown kpi=%s period_type=%s %s→%s "
                "slice_filters=%s run_timestamp=%s period_source=%s",
                kpi_name,
                period_type.value,
                start,
                end,
                slice_filters,
                ts_used,
                period_source,
            )
            raw = await fetch_derived_kpi_breakdown(
                loader,
                db,
                pbi,
                settings,
                period_start=start,
                period_end=end,
                kpi_names=kpi_tuple,
                slice_filters=slice_filters,
            )
            data = _flatten_kpi_breakdown_payload(raw, kpi_name=kpi_name)
            if data.get("period"):
                data["period"]["label"] = win.get("period")
            data["period_source"] = period_source
            if period_source == "main_insights":
                data["run_timestamp"] = ts_used
            data["period_type"] = period_type.value
            return data

        if not ts_used:
            raise HTTPException(404, "No main_insights run found to resolve period dates.")

        monthly_start, monthly_end = parse_period_window_dates(m_win)
        weekly_start, weekly_end = parse_period_window_dates(w_win)
        if monthly_start is None or monthly_end is None:
            raise HTTPException(
                404,
                f"Could not resolve monthly period '{PORTAL_MONTHLY_PERIOD_LABEL}' in main_insights.",
            )
        if weekly_start is None or weekly_end is None:
            raise HTTPException(
                404,
                f"Could not resolve weekly period '{PORTAL_WEEKLY_PERIOD_LABEL}' in main_insights.",
            )

        logger.info(
            "GET /pbi/portal/derived-kpis/breakdown kpi=%s monthly=%s→%s weekly=%s→%s "
            "slice_filters=%s run_timestamp=%s",
            kpi_name,
            monthly_start,
            monthly_end,
            weekly_start,
            weekly_end,
            slice_filters,
            ts_used,
        )
        raw_payload = await fetch_derived_kpi_breakdown_monthly_and_weekly(
            loader,
            db,
            pbi,
            settings,
            monthly_start=monthly_start,
            monthly_end=monthly_end,
            weekly_start=weekly_start,
            weekly_end=weekly_end,
            monthly_period_label=PORTAL_MONTHLY_PERIOD_LABEL,
            weekly_period_label=PORTAL_WEEKLY_PERIOD_LABEL,
            kpi_names=kpi_tuple,
            slice_filters=slice_filters,
        )
        return {
            "kpi_name": kpi_name,
            "run_timestamp": ts_used,
            "slice_filters": raw_payload["monthly"].get("slice_filters") or [],
            "monthly": _flatten_kpi_breakdown_payload(
                raw_payload["monthly"], kpi_name=kpi_name
            ),
            "weekly": _flatten_kpi_breakdown_payload(
                raw_payload["weekly"], kpi_name=kpi_name
            ),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/main-insights/executive-summary",
    response_model=ExecutiveSummaryResponse,
    summary="5-pointer executive summary from maininsightscrm (Azure GPT-4o)",
    description=(
        "Loads rows from **insights.maininsightscrm** (latest ``run_timestamp`` unless specified), "
        "then synthesizes a **five-bullet** executive brief per window using **Azure GPT-4o** "
        "(``ONEPLATFORM_OPENAI__*``).\n\n"
        "- **weekly** — insights with period ``22-28 Jun`` (CRM default week)\n"
        "- **monthly** — insights with period ``1-30 Apr``\n"
        "- omit ``period_type`` → both buckets when data exists\n\n"
        "Each pointer is at most **10 words**."
    ),
)
async def post_main_insights_executive_summary(
    period_type: PeriodTypeQuery = None,
    run_timestamp: Optional[datetime] = Query(
        None,
        description="Batch used for period metadata. Defaults to latest main_insights run_timestamp.",
    ),
    limit: int = Query(500, ge=1, le=5000, description="Max main_insights rows to load for period metadata"),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store: ResultStore = Depends(get_result_store),
):
    """LLM executive summary from ``insights.maininsightscrm`` via Azure GPT-4o."""
    store = _select_main_insights_store(store, main_insights_table)
    logger.info(
        "POST /pbi/portal/main-insights/executive-summary period_type=%s run_timestamp=%s limit=%s",
        period_type.value if period_type else None,
        run_timestamp,
        limit,
    )
    try:
        engine = InsightEngine(store, main_insights_llm=MainInsightsNarrativeModel.azure_default)
        result = await engine.summarize_main_insights_executive_split(
            run_timestamp=run_timestamp,
            limit=limit,
            period_type=period_type.value if period_type else None,
        )
        monthly_data = result.get("monthly")
        weekly_data = result.get("weekly")
        return ExecutiveSummaryResponse(
            run_timestamp=result.get("run_timestamp"),
            period_type=period_type.value if period_type else None,
            monthly=ExecutiveSummaryBucket(**monthly_data) if monthly_data else None,
            weekly=ExecutiveSummaryBucket(**weekly_data) if weekly_data else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/main-insights/{insight_id}/breakdown",
    response_model=dict[str, Any],
    summary="KPI breakdown by CRM dimensions for one main insight",
    description=(
        "Drill-down for the diagnosis UI.\n\n"
        "Returns KPI values grouped by **Potential**, **Potential Type**, "
        "**Opportunity Type**, **Industry Head**, **Stage**, and **Sales Rep** "
        "for the insight's period.\n\n"
        "- **KPI Portfolio** insights (rollup) → portfolio-wide breakdown (no parent TREATAS).\n"
        "- **Slice** insights (e.g. ``stage`` = Discovery) → TREATAS on the parent slice, "
        "then group by the other CRM dimensions.\n\n"
        "Pass **`kpi_name`** to load one KPI at a time (recommended for the UI)."
    ),
)
async def get_main_insight_breakdown(
    insight_id: UUID,
    kpi_name: InsightKpiNameQuery = None,
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store: ResultStore = Depends(get_result_store),
    loader=Depends(get_config_loader),
    db: AsyncSession = Depends(get_db),
    pbi=Depends(get_pbi_client),
    settings=Depends(get_dax_settings),
):
    """Break down one insight slice into child dimension KPI values (same period as insight)."""
    if pbi is None or settings is None:
        raise HTTPException(500, "Power BI settings/client are not configured")
    store = _select_main_insights_store(store, main_insights_table)
    try:
        return await fetch_insight_breakdown(
            store=store,
            loader=loader,
            db=db,
            pbi=pbi,
            settings=settings,
            insight_id=insight_id,
            kpi_name=kpi_name,
        )
    except ValueError as e:
        detail = str(e)
        status = 400 if "is not on this insight" in detail else 404
        raise HTTPException(status_code=status, detail=detail) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
