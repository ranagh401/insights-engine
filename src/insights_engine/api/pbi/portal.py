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
from ...engine.portal_kpi_groups import PORTAL_KPI_GROUPS
from ...store.result_store import MAIN_INSIGHTS_TABLE_QUERY_HELP, ResultStore
from ..dependencies import get_config_loader, get_dax_settings, get_pbi_client, get_result_store

logger = logging.getLogger(__name__)
router = APIRouter()


class PeriodTypeOption(str, Enum):
    """Swagger-visible enum for monthly vs weekly portal windows."""

    monthly = "monthly"
    weekly = "weekly"


class PortalKpiNameOption(str, Enum):
    """Legacy Swagger examples — any ``kpiname`` from configkpisclientportal is accepted."""

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
        description="KPI ``kpiname`` from insights.configkpisclientportal.",
        examples=["commit_slipped_count", "oem_leads", "demo_rate"],
    ),
]

InsightKpiNameQuery = Annotated[
    Optional[str],
    Query(
        title="KPI name",
        description=(
            "Optional: return breakdown for one KPI only (fewer Power BI queries). "
            "Must be a ``kpiname`` in insights.configkpisclientportal. "
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


class GroupBestInsight(BaseModel):
    heading: Optional[str] = Field(
        None, description="Group insight title, wrapped in /h ... /h markers for the frontend."
    )
    description: Optional[str] = Field(
        None,
        description="Plain-text insight synthesized across ALL of the group's insights.",
    )


class ExecutiveSummaryBucket(BaseModel):
    period: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    insight_count: int = 0
    pointers: list[str] = Field(
        default_factory=list,
        description="Five LLM-generated pointers with inline /green or /red color markers.",
    )
    best_insight: Optional[GroupBestInsight] = Field(
        None,
        description="Only when a group is requested: one insight synthesized from all the group's insights.",
    )
    recommended_action: Optional[str] = Field(
        None,
        description="Only when a group is requested: one LLM-generated recommended action for the group.",
    )


class ExecutiveSummaryResponse(BaseModel):
    run_timestamp: Optional[datetime] = None
    period_type: Optional[str] = Field(
        None,
        description="Echo of request: monthly, weekly, or null when both were returned.",
    )
    group: Optional[str] = Field(
        None,
        description="Echo of request: the KPI-card group (group1..group5), or null for overall.",
    )
    monthly: Optional[ExecutiveSummaryBucket] = None
    weekly: Optional[ExecutiveSummaryBucket] = None


class FinalSummaryResponse(BaseModel):
    group: Optional[str] = Field(None, description="Echo of the requested group, or null for overall.")
    source: str = Field(
        ..., description="'stored' when read from insights.finalportal, 'generated' when built via LLM."
    )
    run_timestamp: Optional[datetime] = None
    executive_summary: list[str] = Field(
        default_factory=list,
        description="Executive summary pointers (each may carry an inline /green or /red marker).",
    )
    insight: Optional[GroupBestInsight] = Field(
        None, description="Synthesized group insight (heading wrapped in /h ... /h, plus description)."
    )
    recommended_action: Optional[str] = Field(None, description="One recommended action for the group.")


class SalesRepSummaryResponse(BaseModel):
    sales_rep: str = Field(..., description="Echo of the requested rep.")
    group: Optional[str] = Field(None, description="Echo of the requested group, or null for overall.")
    source: str = Field(
        ...,
        description="'stored' when read from insights.finalportalsalesrep, 'generated' when built via LLM.",
    )
    signal_count: Optional[int] = Field(
        None, description="Rep signals that fed this summary."
    )
    executive_summary: list[str] = Field(
        default_factory=list,
        description="Pointers, each carrying an inline /green or /red marker.",
    )
    insight: Optional[GroupBestInsight] = None
    recommended_action: Optional[str] = None


class SalesRepListItem(BaseModel):
    sales_rep: str
    signal_count: int


class SalesRepRefreshItem(BaseModel):
    sales_rep: str
    group_name: str
    signal_count: int
    pointers: int


class SalesRepRefreshResponse(BaseModel):
    dry_run: bool
    reps: int = Field(..., description="Reps processed.")
    rows: int = Field(..., description="(rep, group) rows written.")
    refreshed: list[SalesRepRefreshItem] = Field(default_factory=list)


class FinalSummaryRefreshItem(BaseModel):
    group_name: str = Field(..., description="Key written to insights.finalportal.")
    insight_count: int = Field(..., description="Main-insights rows that fed this summary.")
    pointers: list[str] = Field(default_factory=list)
    insight: Optional[GroupBestInsight] = None
    recommended_action: Optional[str] = None


class FinalSummaryRefreshResponse(BaseModel):
    run_timestamp: Optional[datetime] = None
    dry_run: bool = Field(..., description="True when nothing was written.")
    refreshed: list[FinalSummaryRefreshItem] = Field(default_factory=list)


class GroupNameBackfillResponse(BaseModel):
    run_timestamp: Optional[datetime] = Field(
        None, description="Batch that was updated, or null when all runs were."
    )
    scanned: int = Field(..., description="Rows examined.")
    changed: int = Field(..., description="Rows whose group_name was set to a new value.")
    dry_run: bool = Field(..., description="True when nothing was written.")
    by_group: dict[str, int] = Field(
        default_factory=dict,
        description="Rows changed per resulting tag; 'NULL' counts rows cleared.",
    )
    unmapped_kpis: list[str] = Field(
        default_factory=list,
        description="KPIs no card claims. Left untouched unless clear_unmapped=true.",
    )


@router.get(
    "/derived-kpis",
    response_model=dict[str, Any],
    summary="Fetch portal KPI tiles from configkpisclientportal",
    description=(
        "Portfolio-level KPI tiles with prior-period comparison. "
        "KPI list and PBI measures come from **insights.configkpisclientportal** "
        "(Portal has no separate derived-KPI table).\n\n"
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
    """Portfolio drill-down for one KPI × Portal breakdown dimensions."""
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
    response_model=FinalSummaryResponse,
    summary="Executive summary per group (stored in insights.finalportal, LLM fallback)",
    description=(
        "Returns the precomputed executive summary for a KPI-card group from "
        "**insights.finalportal** (keyed by group; 'overall' for no group). "
        "``source='stored'`` when served from the table (no LLM call). "
        "When no row exists, falls back to an on-the-fly Azure GPT-4o summary "
        "(``source='generated'``).\n\n"
        "Response: ``executive_summary`` (pointer list with /green|/red markers), "
        "``insight`` (heading in /h ... /h + description), ``recommended_action``."
    ),
)
async def post_main_insights_executive_summary(
    period_type: PeriodTypeQuery = None,
    group: Optional[str] = Query(
        None,
        description=(
            "KPI-card group to scope the summary to, e.g. group1..group5. "
            "Omit for an overall summary. When set, only maininsightsportal rows "
            "whose manual 'group_name' column equals this group are summarized, "
            "and a best_insight + recommended_action are appended per bucket."
        ),
    ),
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
    """Executive summary for a KPI-card group.

    Reads the precomputed row from ``insights.finalportal`` (keyed by group, with
    'overall' for no group). Falls back to an on-the-fly Azure GPT-4o summary
    only when no stored row exists.
    """
    store = _select_main_insights_store(store, main_insights_table)
    group_val = (group or "").strip() or None
    lookup_key = group_val or "overall"
    logger.info(
        "POST /pbi/portal/main-insights/executive-summary group=%s (lookup=%s) period_type=%s",
        group_val,
        lookup_key,
        period_type.value if period_type else None,
    )

    # 1) Stored (no LLM call).
    try:
        stored = await store.get_finalportal_by_group(lookup_key)
    except Exception:
        stored = None
    if stored and any(
        stored.get(k) for k in ("executive_summary", "insight", "recommended_action")
    ):
        es = stored.get("executive_summary")
        ins = stored.get("insight")
        return FinalSummaryResponse(
            group=group_val,
            source="stored",
            executive_summary=es if isinstance(es, list) else [],
            insight=GroupBestInsight(**ins) if isinstance(ins, dict) else None,
            recommended_action=stored.get("recommended_action"),
        )

    # 2) LLM fallback — generate, then flatten the relevant period bucket.
    try:
        engine = InsightEngine(store, main_insights_llm=MainInsightsNarrativeModel.azure_default)
        result = await engine.summarize_main_insights_executive_split(
            run_timestamp=run_timestamp,
            limit=limit,
            period_type=period_type.value if period_type else None,
            group=group_val,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    pt = period_type.value if period_type else None
    bucket = (
        result.get("monthly") if pt == "monthly" else result.get("weekly")
    ) or result.get("weekly") or result.get("monthly") or {}
    ins = bucket.get("best_insight")
    return FinalSummaryResponse(
        group=group_val,
        source="generated",
        run_timestamp=result.get("run_timestamp"),
        executive_summary=bucket.get("pointers", []),
        insight=GroupBestInsight(**ins) if isinstance(ins, dict) else None,
        recommended_action=bucket.get("recommended_action"),
    )


class CurrentPeriodResponse(BaseModel):
    period_start: str = Field(..., description="Inclusive window start, YYYY-MM-DD.")
    period_end: str = Field(..., description="Inclusive window end, YYYY-MM-DD.")
    period_label: Optional[str] = Field(None, description="Human label, e.g. '15-21 Jul'.")
    updated_at: Optional[datetime] = None


@router.get(
    "/current-period",
    response_model=CurrentPeriodResponse,
    summary="Current reporting window applied by the weekly refresh",
    description=(
        "The reporting window the weekly refresh job last applied, from "
        "**insights.portal_current_period**. The frontend reads this at runtime instead of "
        "hardcoding the KPI-card window, so refreshing the period needs no frontend redeploy.\n\n"
        "`404` until the first refresh has run."
    ),
)
async def get_current_period(store: ResultStore = Depends(get_result_store)):
    """Serve the current reporting window for the frontend."""
    try:
        row = await store.get_current_period()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    if not row:
        raise HTTPException(
            status_code=404, detail="No current period set yet; the weekly refresh has not run."
        )
    return CurrentPeriodResponse(
        period_start=str(row["period_start"]),
        period_end=str(row["period_end"]),
        period_label=row.get("period_label"),
        updated_at=row.get("updated_at"),
    )


@router.get(
    "/main-insights/sales-reps",
    response_model=list[SalesRepListItem],
    summary="Sales reps that have signals, busiest first",
    description=(
        "Reps present in **insights.signal_log** under `dimension='sales_rep'`, with their "
        "signal counts. Use this to drive a rep picker; it is the population the rep-level "
        "executive summaries are built from."
    ),
)
async def get_sales_reps(
    min_signals: int = Query(1, ge=1, description="Only reps with at least this many signals."),
    store: ResultStore = Depends(get_result_store),
):
    """Reps available for rep-level executive summaries."""
    try:
        rows = await store.list_sales_reps_with_signals(min_signals=min_signals)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return [SalesRepListItem(**r) for r in rows if (r["sales_rep"] or "").strip()]


@router.post(
    "/main-insights/executive-summary/sales-rep",
    response_model=SalesRepSummaryResponse,
    summary="Executive summary per sales rep (stored in insights.finalportalsalesrep, LLM fallback)",
    description=(
        "Returns the precomputed executive summary for one **sales rep**, optionally scoped to a "
        "KPI-card group, from **insights.finalportalsalesrep** (keyed by rep + group; 'overall' for "
        "no group). ``source='stored'`` when served from the table (no LLM call).\n\n"
        "When no row exists, falls back to generating from the rep's **signal_log** rows "
        "(``source='generated'``) — rep-level data does not exist in maininsightsportal.\n\n"
        "Response mirrors the group executive summary: ``executive_summary`` (pointers with "
        "/green|/red markers), ``insight`` (heading in /h ... /h + description), ``recommended_action``."
    ),
)
async def post_sales_rep_executive_summary(
    sales_rep: str = Query(..., description="Rep name, e.g. `Uday Singh`. Matched case-insensitively."),
    group: Optional[str] = Query(
        None,
        description="KPI-card group to scope to, e.g. group1..group5. Omit for the rep's overall summary.",
    ),
    limit: int = Query(500, ge=1, le=5000, description="Max rep signals to load."),
    store: ResultStore = Depends(get_result_store),
):
    """Executive summary for one sales rep."""
    rep = (sales_rep or "").strip()
    if not rep:
        raise HTTPException(status_code=400, detail="sales_rep is required")
    group_val = (group or "").strip() or None
    if group_val and group_val.lower() not in PORTAL_KPI_GROUPS:
        raise HTTPException(status_code=400, detail=f"unknown group: {group_val}")
    lookup_key = group_val or "overall"
    logger.info(
        "POST /pbi/portal/main-insights/executive-summary/sales-rep rep=%s group=%s (lookup=%s)",
        rep,
        group_val,
        lookup_key,
    )

    # 1) Stored (no LLM call).
    try:
        stored = await store.get_finalportalsalesrep(rep, lookup_key)
    except Exception:
        stored = None
    if stored and any(
        stored.get(k) for k in ("executive_summary", "insight", "recommended_action")
    ):
        es = stored.get("executive_summary")
        ins = stored.get("insight")
        return SalesRepSummaryResponse(
            sales_rep=stored.get("sales_rep") or rep,
            group=group_val,
            source="stored",
            signal_count=stored.get("signal_count"),
            executive_summary=es if isinstance(es, list) else [],
            insight=GroupBestInsight(**ins) if isinstance(ins, dict) else None,
            recommended_action=stored.get("recommended_action"),
        )

    # 2) LLM fallback — generate from the rep's signals.
    try:
        engine = InsightEngine(store, main_insights_llm=MainInsightsNarrativeModel.azure_default)
        result = await engine.summarize_sales_rep_executive(
            sales_rep=rep, group=group_val, limit=limit
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    ins = result.get("best_insight")
    return SalesRepSummaryResponse(
        sales_rep=rep,
        group=group_val,
        source="generated",
        signal_count=int(result.get("signal_count") or 0),
        executive_summary=result.get("pointers", []),
        insight=GroupBestInsight(**ins) if isinstance(ins, dict) else None,
        recommended_action=result.get("recommended_action"),
    )


@router.post(
    "/main-insights/executive-summary/sales-rep/refresh",
    response_model=SalesRepRefreshResponse,
    summary="Regenerate insights.finalportalsalesrep from signal_log",
    description=(
        "Runs the Azure GPT-4o generator per (rep, group) and upserts into "
        "**insights.finalportalsalesrep** so later reads are served as ``source='stored'``.\n\n"
        "Expensive: two GPT-4o calls per group row plus one per rep 'overall'. Scope it with "
        "`sales_reps`, `groups` and `min_signals`, or preview with `dry_run=true`."
    ),
)
async def post_sales_rep_executive_summary_refresh(
    sales_reps: Optional[str] = Query(
        None, description="Comma-separated rep names. Defaults to every rep meeting min_signals."
    ),
    groups: Optional[str] = Query(
        None, description="Comma-separated keys, e.g. `group1,overall`. Defaults to group1..group5 plus 'overall'."
    ),
    min_signals: int = Query(1, ge=1, description="Skip reps with fewer signals than this."),
    limit: int = Query(500, ge=1, le=5000, description="Max rep signals to load per call."),
    dry_run: bool = Query(False, description="Report what would be written without calling the LLM."),
    store: ResultStore = Depends(get_result_store),
):
    """Regenerate and store rep-level executive summaries."""
    try:
        available = await store.list_sales_reps_with_signals(min_signals=min_signals)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    counts = {r["sales_rep"]: r["signal_count"] for r in available if (r["sales_rep"] or "").strip()}

    wanted = [r.strip() for r in (sales_reps or "").split(",") if r.strip()]
    reps = wanted or sorted(counts, key=lambda k: -counts[k])
    unknown = [r for r in reps if r not in counts]
    if unknown:
        raise HTTPException(status_code=400, detail=f"no signals for: {', '.join(unknown)}")

    keys = [g.strip().lower() for g in (groups or "").split(",") if g.strip()] or [
        *PORTAL_KPI_GROUPS.keys(),
        "overall",
    ]
    bad = [k for k in keys if k != "overall" and k not in PORTAL_KPI_GROUPS]
    if bad:
        raise HTTPException(status_code=400, detail=f"unknown group(s): {', '.join(bad)}")

    engine = InsightEngine(store, main_insights_llm=MainInsightsNarrativeModel.azure_default)
    out: list[SalesRepRefreshItem] = []
    for rep in reps:
        for key in keys:
            group_val = None if key == "overall" else key
            if dry_run:
                out.append(
                    SalesRepRefreshItem(
                        sales_rep=rep, group_name=key, signal_count=counts[rep], pointers=0
                    )
                )
                continue
            try:
                result = await engine.summarize_sales_rep_executive(
                    sales_rep=rep, group=group_val, limit=limit
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"{rep}/{key}: {e}") from e
            pointers = result.get("pointers") or []
            ins = result.get("best_insight")
            await store.upsert_finalportalsalesrep(
                rep,
                key,
                executive_summary=pointers,
                insight=ins if isinstance(ins, dict) else None,
                recommended_action=result.get("recommended_action"),
                signal_count=int(result.get("signal_count") or 0),
            )
            out.append(
                SalesRepRefreshItem(
                    sales_rep=rep,
                    group_name=key,
                    signal_count=int(result.get("signal_count") or 0),
                    pointers=len(pointers),
                )
            )
    logger.info(
        "finalportalsalesrep refresh | reps=%s rows=%s dry_run=%s", len(reps), len(out), dry_run
    )
    return SalesRepRefreshResponse(
        dry_run=dry_run, reps=len(reps), rows=len(out), refreshed=out
    )


@router.post(
    "/main-insights/executive-summary/refresh",
    response_model=FinalSummaryRefreshResponse,
    summary="Regenerate insights.finalportal from current main-insights rows",
    description=(
        "Runs the same Azure GPT-4o generator the executive-summary endpoint falls back "
        "to, once per KPI-card group, and upserts the result into **insights.finalportal** "
        "so later reads are served as ``source='stored'`` with no LLM call.\n\n"
        "Run this after loading a new batch of main-insights rows — stored rows win over "
        "generation, so a stale finalportal is served indefinitely otherwise.\n\n"
        "Costs two GPT-4o calls per group (pointers + heading/action), one for 'overall' "
        "(pointers only, matching the generated path). Pass `dry_run=true` to preview "
        "without writing."
    ),
)
async def post_main_insights_executive_summary_refresh(
    groups: Optional[str] = Query(
        None,
        description=(
            "Comma-separated keys to refresh, e.g. `group1,group5`. "
            "Defaults to group1..group5 plus 'overall'."
        ),
    ),
    period_type: PeriodTypeQuery = None,
    run_timestamp: Optional[datetime] = Query(
        None, description="Batch to summarize. Defaults to the latest main-insights run."
    ),
    limit: int = Query(500, ge=1, le=5000, description="Max main-insights rows to load."),
    dry_run: bool = Query(False, description="Generate and return without writing."),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store: ResultStore = Depends(get_result_store),
):
    """Regenerate and store the executive summary for each KPI-card group."""
    store = _select_main_insights_store(store, main_insights_table)
    keys = [g.strip().lower() for g in (groups or "").split(",") if g.strip()] or [
        *PORTAL_KPI_GROUPS.keys(),
        "overall",
    ]
    pt = period_type.value if period_type else None
    engine = InsightEngine(store, main_insights_llm=MainInsightsNarrativeModel.azure_default)

    refreshed: list[FinalSummaryRefreshItem] = []
    for key in keys:
        group_val = None if key == "overall" else key
        try:
            result = await engine.summarize_main_insights_executive_split(
                run_timestamp=run_timestamp,
                limit=limit,
                period_type=pt,
                group=group_val,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"{key}: {e}") from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{key}: {e}") from e

        bucket = (
            result.get("monthly") if pt == "monthly" else result.get("weekly")
        ) or result.get("weekly") or result.get("monthly") or {}
        pointers = bucket.get("pointers") or []
        ins = bucket.get("best_insight")
        action = bucket.get("recommended_action")
        if not dry_run:
            await store.upsert_finalportal(
                key,
                executive_summary=pointers,
                insight=ins if isinstance(ins, dict) else None,
                recommended_action=action,
            )
        refreshed.append(
            FinalSummaryRefreshItem(
                group_name=key,
                insight_count=int(bucket.get("insight_count") or 0),
                pointers=pointers,
                insight=GroupBestInsight(**ins) if isinstance(ins, dict) else None,
                recommended_action=action,
            )
        )
        logger.info(
            "finalportal refresh | group=%s insights=%s pointers=%s dry_run=%s",
            key,
            bucket.get("insight_count"),
            len(pointers),
            dry_run,
        )

    return FinalSummaryRefreshResponse(
        run_timestamp=result.get("run_timestamp"),
        dry_run=dry_run,
        refreshed=refreshed,
    )


@router.post(
    "/main-insights/backfill-group-names",
    response_model=GroupNameBackfillResponse,
    summary="Set group_name on main-insights rows from the KPI-card group map",
    description=(
        "Fills the manual **group_name** column from `PORTAL_KPI_GROUPS`, matching each "
        "row's `kpi` (falling back to `kpi_family`) to the cards that name it. "
        "`write_main_insights` does not map this column, so engine-written rows land "
        "ungrouped and every group-scoped executive summary matches zero insights.\n\n"
        "A KPI claimed by several cards gets a comma-separated tag (e.g. `group1,group4`), "
        "which is what `row_in_group` reads back. KPIs in no card are reported under "
        "`unmapped_kpis` and left untouched unless `clear_unmapped=true`.\n\n"
        "Defaults to the latest run. Pass `dry_run=true` to preview the counts first."
    ),
)
async def post_main_insights_backfill_group_names(
    run_timestamp: Optional[datetime] = Query(
        None,
        description="Scope to one batch. Defaults to the latest run unless all_runs=true.",
    ),
    all_runs: bool = Query(
        False, description="Update every run in the table instead of just the latest."
    ),
    overwrite: bool = Query(
        True,
        description="Replace existing group_name values. False only fills rows where it is null.",
    ),
    clear_unmapped: bool = Query(
        False,
        description="Set group_name to null for KPIs no card claims, instead of leaving them as-is.",
    ),
    dry_run: bool = Query(
        False, description="Report what would change without writing."
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store: ResultStore = Depends(get_result_store),
):
    """Backfill ``group_name`` from the KPI-card group map."""
    store = _select_main_insights_store(store, main_insights_table)
    ts = run_timestamp
    if ts is None and not all_runs:
        ts = await store.get_latest_main_insight_run_timestamp()
        if ts is None:
            raise HTTPException(
                status_code=404,
                detail="No main-insights rows found; nothing to backfill.",
            )
    logger.info(
        "POST /pbi/portal/main-insights/backfill-group-names run_timestamp=%s "
        "all_runs=%s overwrite=%s clear_unmapped=%s dry_run=%s",
        ts,
        all_runs,
        overwrite,
        clear_unmapped,
        dry_run,
    )
    try:
        result = await store.backfill_group_names(
            run_timestamp=ts,
            overwrite=overwrite,
            clear_unmapped=clear_unmapped,
            dry_run=dry_run,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return GroupNameBackfillResponse(run_timestamp=ts, **result)


@router.get(
    "/main-insights/{insight_id}/breakdown",
    response_model=dict[str, Any],
    summary="KPI breakdown by Portal dimensions for one main insight",
    description=(
        "Drill-down for the diagnosis UI.\n\n"
        "Returns KPI values grouped by **Potential**, **Potential Type**, "
        "**Opportunity Type**, **Industry Head**, **Stage**, and **Sales Rep** "
        "for the insight's period.\n\n"
        "- **KPI Portfolio** insights (rollup) → portfolio-wide breakdown (no parent TREATAS).\n"
        "- **Slice** insights (e.g. ``stage`` = Discovery) → TREATAS on the parent slice, "
        "then group by the other Portal dimensions.\n\n"
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
