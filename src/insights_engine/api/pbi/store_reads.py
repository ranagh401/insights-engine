"""Read-only access to operational Postgres tables (signal_log, why_results, main_insights)."""

from __future__ import annotations

from typing import Any, Optional

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from ..dependencies import get_result_store
from ...store.result_store import ResultStore

router = APIRouter()


@router.get(
    "/signal-log",
    response_model=list[dict[str, Any]],
    summary="List generated signals (insights.signal_log)",
)
async def list_signal_log(
    limit: int = Query(500, ge=1, le=5000, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    store: ResultStore = Depends(get_result_store),
):
    """Equivalent to ``SELECT * FROM insights.signal_log ORDER BY detected_at DESC`` (paginated)."""
    return await store.list_signal_log_rows(limit=limit, offset=offset)


@router.get(
    "/why-results",
    response_model=list[dict[str, Any]],
    summary="List generated WHY rows (insights.why_results)",
)
async def list_why_results(
    limit: int = Query(500, ge=1, le=5000, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    store: ResultStore = Depends(get_result_store),
):
    """Equivalent to ``SELECT * FROM insights.why_results ORDER BY created_at DESC`` (paginated)."""
    return await store.list_why_result_rows(limit=limit, offset=offset)


@router.get(
    "/main-insights",
    response_model=list[dict[str, Any]],
    summary="List main insights (insights.main_insights)",
)
async def list_main_insights(
    limit: int = Query(500, ge=1, le=5000, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    run_timestamp: Optional[datetime] = Query(
        None,
        description="If set, only rows with this run_timestamp.",
    ),
    kpi_family: Optional[str] = Query(None, description="If set, filter by kpi_family."),
    pascal_case: bool = Query(
        True,
        description="If true (default), keys match vw_main_insights (InsightID, RunTimestamp, …).",
    ),
    store: ResultStore = Depends(get_result_store),
):
    """Newest first. Postgres view ``insights.vw_main_insights`` exposes the same columns for SQL clients."""
    return await store.list_main_insight_rows(
        limit=limit,
        offset=offset,
        pascal_case=pascal_case,
        run_timestamp=run_timestamp,
        kpi_family=kpi_family,
    )
