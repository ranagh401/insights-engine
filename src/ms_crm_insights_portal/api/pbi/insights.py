"""Endpoints for Azure OpenAI main insight generation and related tools."""

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...config.narrative_llm import MainInsightsNarrativeModel
from ...engine.insight_engine import InsightEngine
from ...store.result_store import MAIN_INSIGHTS_TABLE_QUERY_HELP, ResultStore
from ..dependencies import get_result_store

logger = logging.getLogger(__name__)
router = APIRouter()


def _select_main_insights_store(store: ResultStore, table_name: Optional[str]) -> ResultStore:
    """Scope a request to one supported main-insights physical table."""
    try:
        return store.with_main_insights_table(table_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


async def _background_dimensional_clustering(
    store: ResultStore,
    *,
    all_timestamps: bool,
    target_dimensions: Optional[list[str]],
) -> None:
    """Runs after main insights; logs errors without failing the original request."""
    try:
        engine = InsightEngine(store)
        n, ts, skipped = await engine.cluster_signals_dimensional(
            None,
            target_dimensions,
            all_timestamps,
        )
        logger.info(
            "[chained] Dimensional main insights finished | persisted=%s run_timestamp=%s skipped=%s",
            n,
            ts,
            len(skipped),
        )
        if skipped:
            logger.warning(
                "[chained] Dimensional clustering skipped slices (no chunking): %s",
                skipped[:50],
            )
    except Exception:
        logger.exception("[chained] Dimensional clustering failed (see stack trace above)")

class InsightResponse(BaseModel):
    message: str
    insights_generated: int = 0
    recommended_actions_updated: int = 0
    run_timestamp: Optional[datetime] = None
    skipped_groups: list[str] = Field(
        default_factory=list,
        description="Skipped KPI×dimension or dimensional slices (e.g. WHY text over size limit). Human-readable reasons.",
    )


class MainInsightFeedbackBody(BaseModel):
    """Partial update: only fields you send are written (omit keys to leave unchanged)."""

    like: Optional[bool] = Field(None, description="User like flag (null clears if sent explicitly as null in JSON)")
    dislike: Optional[bool] = Field(None, description="User dislike flag")
    remarks: Optional[str] = Field(None, description="Free-text remarks")
    park: Optional[bool] = Field(None, description="Park / save-for-later flag (true or false)")


class RecommendedActionsResponse(BaseModel):
    message: str
    updated: int = 0
    run_timestamp: Optional[datetime] = None


class ReformatMarkupResponse(BaseModel):
    """Response for markup reformat (default: LLM **…** pass with deterministic fallback)."""

    message: str
    updated: int = 0


class WhySummaryResponse(BaseModel):
    message: str
    updated: int = 0
    run_timestamp: Optional[datetime] = None


@router.post(
    "/main-insights/generate-standard",
    response_model=InsightResponse,
    summary="Generate standard main insights (one LLM per KPI × analytical dimension)",
)
async def post_generate_standard_main_insights(
    run_timestamp: Optional[datetime] = Query(
        None,
        description=(
            "If set and all_timestamps is false, only signals with this exact detected_at. "
            "If omitted and all_timestamps is false, uses the latest batch per (kpi_name, dimension)."
        ),
    ),
    all_timestamps: bool = Query(
        True,
        description=(
            "If true (default), every row in signal_log is loaded; run_timestamp is ignored for the load. "
            "Batch timestamp is max(detected_at) in that set. Set false for a single batch."
        ),
    ),
    kpi_names: Optional[list[str]] = Query(
        None,
        description=(
            "Optional allow-list of ``signal_log.kpi_name`` — repeat this query param once per KPI "
            "(case-insensitive)."
        ),
    ),
    main_insights_llm: MainInsightsNarrativeModel = Query(
        MainInsightsNarrativeModel.azure_default,
        description=(
            "Narrative LLM: ``azure_default`` (``PLATFORM_OPENAI__*``), "
            "``azure_gpt54_mini`` (``INSIGHTS_AZURE_GPT54_MINI__*``), or "
            "``cohere_command_a_plus`` (``INSIGHTS_COHERE_AZURE__*``)."
        ),
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store=Depends(get_result_store),
):
    """Persists **main_insights** from ``signal_log`` + ``why_results`` (one-shot LLM per KPI × dimension)."""
    store = _select_main_insights_store(store, main_insights_table)
    logger.info(
        "POST /pbi/insights/main-insights/generate-standard started (all_timestamps=%s run_timestamp=%s kpi_names=%s main_insights_llm=%s)",
        all_timestamps,
        run_timestamp,
        kpi_names,
        main_insights_llm.value,
    )
    try:
        engine = InsightEngine(store, main_insights_llm=main_insights_llm)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        count, ts_used, skipped = await engine.generate_standard_main_insights(
            run_timestamp, all_timestamps, kpi_names=kpi_names
        )
        msg = "Successfully generated standard main insights"
        if skipped:
            msg += f" ({len(skipped)} group(s) skipped — see skipped_groups)"
        logger.info(
            "POST /pbi/insights/main-insights/generate-standard finished: insights=%s run_timestamp=%s skipped=%s",
            count,
            ts_used,
            len(skipped),
        )
        return InsightResponse(
            message=msg,
            insights_generated=count,
            run_timestamp=ts_used,
            skipped_groups=skipped,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/main-insights/generate-dimensional",
    response_model=InsightResponse,
    summary="Generate dimensional (cross-KPI) main insights for slice dimensions",
)
async def post_generate_dimensional_main_insights(
    run_timestamp: Optional[datetime] = Query(
        None,
        description=(
            "If set and all_timestamps is false, only signals with this exact detected_at. "
            "If omitted and all_timestamps is false, uses the latest single detected_at from signal_log."
        ),
    ),
    all_timestamps: bool = Query(
        True,
        description="If true (default), load all signals; else restrict to run_timestamp or latest batch.",
    ),
    target_dimensions: Optional[list[str]] = Query(
        None,
        description=(
            "Allow-list of ``signal_log.dimension`` values (repeat query param or comma-separated). "
            "If omitted, defaults to Division, Market_Type, Lead_Source_Group. "
            "Clusters are one row per (dimension value × distinct WHY period window from why_results)."
        ),
    ),
    main_insights_llm: MainInsightsNarrativeModel = Query(
        MainInsightsNarrativeModel.azure_default,
        description=(
            "Narrative LLM: ``azure_default``, ``azure_gpt54_mini``, or "
            "``cohere_command_a_plus`` (same env vars as generate-standard)."
        ),
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store=Depends(get_result_store),
):
    """One **main_insight** per ``(dimension, dimension_value, WHY period bucket)``.

    Buckets match distinct ``(period_start, period_end)`` in ``why_results`` (e.g. weekly **15-21 Mar**
    vs monthly **1-21 Mar** for the same slice). Each insight combines every KPI on that slice that
    has WHY rows in that window.
    """
    store = _select_main_insights_store(store, main_insights_table)
    logger.info(
        "POST /pbi/insights/main-insights/generate-dimensional started (all_timestamps=%s run_timestamp=%s main_insights_llm=%s)",
        all_timestamps,
        run_timestamp,
        main_insights_llm.value,
    )
    try:
        engine = InsightEngine(store, main_insights_llm=main_insights_llm)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        count, ts_used, skipped = await engine.cluster_signals_dimensional(
            run_timestamp, target_dimensions, all_timestamps
        )
        msg = "Successfully generated dimensional main insights"
        if skipped:
            msg += f" ({len(skipped)} slice(s) skipped — see skipped_groups)"
        logger.info(
            "POST /pbi/insights/main-insights/generate-dimensional finished: insights=%s run_timestamp=%s skipped=%s",
            count,
            ts_used,
            len(skipped),
        )
        return InsightResponse(
            message=msg,
            insights_generated=count,
            run_timestamp=ts_used,
            skipped_groups=skipped,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/main-insights/generate-kpi",
    response_model=InsightResponse,
    summary="Generate KPI rollup main insights (one LLM per config KPI)",
)
async def post_generate_kpi_rollup_main_insights(
    run_timestamp: Optional[datetime] = Query(
        None,
        description=(
            "If set and all_timestamps is false, only signals with this exact detected_at. "
            "If omitted and all_timestamps is false, uses the latest batch per (kpi_name, dimension)."
        ),
    ),
    all_timestamps: bool = Query(
        True,
        description=(
            "If true (default), every row in signal_log is loaded; run_timestamp is ignored for the load. "
            "Set false for a single batch."
        ),
    ),
    kpi_names: Optional[list[str]] = Query(
        None,
        description=(
            "Optional allow-list of ``signal_log.kpi_name`` — repeat this query param once per KPI "
            "(case-insensitive)."
        ),
    ),
    main_insights_llm: MainInsightsNarrativeModel = Query(
        MainInsightsNarrativeModel.azure_default,
        description=(
            "Narrative LLM: ``azure_default``, ``azure_gpt54_mini``, or "
            "``cohere_command_a_plus`` (same env vars as generate-standard)."
        ),
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store=Depends(get_result_store),
):
    """One **main_insight** per KPI in **configkpisclientcrm** (all dimension slices merged).

    KPIs with rows in ``signal_log`` are sent to the LLM. WHY rows are optional — run the WHY
    sweep for richer ``why`` / ``why_insight_summary`` narratives. KPIs with no fired signals
    are returned in ``skipped_groups``. Persisted row shape matches **generate-dimensional**.
    """
    store = _select_main_insights_store(store, main_insights_table)
    logger.info(
        "POST /pbi/insights/main-insights/generate-kpi started (all_timestamps=%s run_timestamp=%s kpi_names=%s main_insights_llm=%s)",
        all_timestamps,
        run_timestamp,
        kpi_names,
        main_insights_llm.value,
    )
    try:
        engine = InsightEngine(store, main_insights_llm=main_insights_llm)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        count, ts_used, skipped = await engine.generate_kpi_rollup_main_insights(
            run_timestamp, all_timestamps, kpi_names=kpi_names
        )
        msg = "Successfully generated KPI rollup main insights"
        if skipped:
            msg += f" ({len(skipped)} KPI bucket(s) skipped — see skipped_groups)"
        logger.info(
            "POST /pbi/insights/main-insights/generate-kpi finished: insights=%s run_timestamp=%s skipped=%s",
            count,
            ts_used,
            len(skipped),
        )
        return InsightResponse(
            message=msg,
            insights_generated=count,
            run_timestamp=ts_used,
            skipped_groups=skipped,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/main-insights",
    response_model=list[dict[str, Any]],
    summary="List persisted main insights",
)
async def get_main_insights(
    limit: int = Query(500, ge=1, le=5000, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    run_timestamp: Optional[datetime] = Query(
        None,
        description="If set, only rows with this run_timestamp (use the value returned by POST /pbi/insights/run-pull-pipeline, with or without its query param).",
    ),
    kpi_family: Optional[str] = Query(
        None,
        description="If set, filter to this KPI family / main KPI name prefix.",
    ),
    pascal_case: bool = Query(
        True,
        description="If true (default), JSON keys match vw_main_insights (InsightID, RunTimestamp, …).",
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store=Depends(get_result_store),
):
    """Returns rows from ``insights.main_insights`` (via the store), newest ``created_at`` first.

    Same data as ``GET /pbi/store/main-insights``; this route lives under ``/insights`` for convenience.
    """
    store = _select_main_insights_store(store, main_insights_table)
    return await store.list_main_insight_rows(
        limit=limit,
        offset=offset,
        pascal_case=pascal_case,
        run_timestamp=run_timestamp,
        kpi_family=kpi_family,
    )


@router.post(
    "/main-insights/recommended-actions",
    response_model=RecommendedActionsResponse,
    summary="Generate recommended_actions (LLM) for main insights",
)
async def post_main_insights_recommended_actions(
    run_timestamp: Optional[datetime] = Query(
        None,
        description="Process all main_insights rows with this run_timestamp. Ignored if insight_id is set.",
    ),
    insight_id: Optional[UUID] = Query(
        None,
        description="If set, only this insight row is processed.",
    ),
    skip_existing: bool = Query(
        False,
        description="If true, skip rows that already have a non-empty recommended_actions value.",
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store=Depends(get_result_store),
):
    """Calls Azure OpenAI using insight, why, summaries, and impact text; writes three comma-separated actions per row."""
    store = _select_main_insights_store(store, main_insights_table)
    logger.info(
        "POST /pbi/insights/main-insights/recommended-actions run_timestamp=%s insight_id=%s skip_existing=%s",
        run_timestamp,
        insight_id,
        skip_existing,
    )
    engine = InsightEngine(store)
    try:
        count, ts_used = await engine.generate_recommended_actions(
            run_timestamp=run_timestamp,
            insight_id=insight_id,
            skip_existing=skip_existing,
        )
        return RecommendedActionsResponse(
            message=f"Updated recommended_actions on {count} row(s)",
            updated=count,
            run_timestamp=ts_used,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/main-insights/summarize-why-sonnet",
    response_model=WhySummaryResponse,
    summary="Summarize main_insights WHY field using Cohere Command A+",
)
async def post_main_insights_summarize_why_sonnet(
    run_timestamp: Optional[datetime] = Query(
        None,
        description="If set, summarize all rows for this run_timestamp. Ignored when insight_id is set.",
    ),
    insight_id: Optional[UUID] = Query(
        None,
        description="If set, summarize WHY only for this insight row.",
    ),
    limit: int = Query(
        2000,
        ge=1,
        le=10_000,
        description="Max rows to process when insight_id is not provided.",
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store=Depends(get_result_store),
):
    """Rewrite only ``why`` for main_insights rows using Cohere Command A+."""
    store = _select_main_insights_store(store, main_insights_table)
    logger.info(
        "POST /pbi/insights/main-insights/summarize-why-sonnet run_timestamp=%s insight_id=%s limit=%s",
        run_timestamp,
        insight_id,
        limit,
    )
    try:
        engine = InsightEngine(
            store, main_insights_llm=MainInsightsNarrativeModel.cohere_command_a_plus
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        updated, ts_used = await engine.summarize_main_insight_why_with_sonnet(
            run_timestamp=run_timestamp,
            insight_id=insight_id,
            limit=limit,
        )
        return WhySummaryResponse(
            message=f"Summarized WHY on {updated} row(s)",
            updated=updated,
            run_timestamp=ts_used,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/main-insights/refine-what-opus",
    response_model=WhySummaryResponse,
    summary="Refine problem_statement + insight_summary using Cohere Command A+",
)
async def post_refine_insight_what_opus(
    run_timestamp: Optional[datetime] = Query(
        None,
        description="If set, refine all rows for this run_timestamp. Ignored when insight_id is set.",
    ),
    insight_id: Optional[UUID] = Query(
        None,
        description="If set, refine only this specific insight row.",
    ),
    limit: int = Query(
        2000,
        ge=1,
        le=10_000,
        description="Max rows to process when insight_id is not provided.",
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store=Depends(get_result_store),
):
    """Refine ``insight`` (problem_statement) and ``insight_summary`` using Cohere Command A+ for language quality."""
    store = _select_main_insights_store(store, main_insights_table)
    logger.info(
        "POST /pbi/insights/main-insights/refine-what-opus run_timestamp=%s insight_id=%s limit=%s",
        run_timestamp, insight_id, limit,
    )
    try:
        engine = InsightEngine(
            store, main_insights_llm=MainInsightsNarrativeModel.cohere_command_a_plus
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        updated, ts_used = await engine.refine_insight_what_with_opus(
            run_timestamp=run_timestamp,
            insight_id=insight_id,
            limit=limit,
        )
        return WhySummaryResponse(
            message=f"Refined insight + insight_summary on {updated} row(s) using Cohere Command A+",
            updated=updated,
            run_timestamp=ts_used,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/main-insights/refine-why-opus",
    response_model=WhySummaryResponse,
    summary="Refine why + why_insight_summary using Cohere Command A+",
)
async def post_refine_insight_why_opus(
    run_timestamp: Optional[datetime] = Query(
        None,
        description="If set, refine all rows for this run_timestamp. Ignored when insight_id is set.",
    ),
    insight_id: Optional[UUID] = Query(
        None,
        description="If set, refine only this specific insight row.",
    ),
    limit: int = Query(
        2000,
        ge=1,
        le=10_000,
        description="Max rows to process when insight_id is not provided.",
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store=Depends(get_result_store),
):
    """Refine ``why`` and ``why_insight_summary`` using Cohere Command A+ for language quality and formatting."""
    store = _select_main_insights_store(store, main_insights_table)
    logger.info(
        "POST /pbi/insights/main-insights/refine-why-opus run_timestamp=%s insight_id=%s limit=%s",
        run_timestamp, insight_id, limit,
    )
    try:
        engine = InsightEngine(
            store, main_insights_llm=MainInsightsNarrativeModel.cohere_command_a_plus
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        updated, ts_used = await engine.refine_insight_why_with_opus(
            run_timestamp=run_timestamp,
            insight_id=insight_id,
            limit=limit,
        )
        return WhySummaryResponse(
            message=f"Refined why + why_insight_summary on {updated} row(s) using Cohere Command A+",
            updated=updated,
            run_timestamp=ts_used,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/main-insights/refine-summary-opus",
    response_model=WhySummaryResponse,
    summary="Rewrite insight_summary into concise CFO-style bullets using Cohere Command A+",
)
async def post_refine_insight_summary_opus(
    run_timestamp: Optional[datetime] = Query(
        None,
        description="If set, refine all rows for this run_timestamp. Ignored when insight_id is set.",
    ),
    insight_id: Optional[UUID] = Query(
        None,
        description="If set, refine only this specific insight row.",
    ),
    limit: int = Query(
        2000,
        ge=1,
        le=10_000,
        description="Max rows to process when insight_id is not provided.",
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store=Depends(get_result_store),
):
    """Rewrite ``insight_summary`` into concise CFO-style bullets using Cohere Command A+."""
    store = _select_main_insights_store(store, main_insights_table)
    logger.info(
        "POST /pbi/insights/main-insights/refine-summary-opus run_timestamp=%s insight_id=%s limit=%s",
        run_timestamp, insight_id, limit,
    )
    try:
        engine = InsightEngine(
            store, main_insights_llm=MainInsightsNarrativeModel.cohere_command_a_plus
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        updated, ts_used = await engine.refine_insight_summary_with_opus(
            run_timestamp=run_timestamp,
            insight_id=insight_id,
            limit=limit,
        )
        return WhySummaryResponse(
            message=f"Refined insight_summary on {updated} row(s) using Cohere Command A+",
            updated=updated,
            run_timestamp=ts_used,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/main-insights/reformat-why-structure-sonnet",
    response_model=WhySummaryResponse,
    summary="Reformat main_insights WHY for portal layout (Cohere Command A+)",
)
async def post_main_insights_reformat_why_structure_sonnet(
    run_timestamp: Optional[datetime] = Query(
        None,
        description="If set, reformat WHY for all rows with this run_timestamp. Ignored when insight_id is set.",
    ),
    insight_id: Optional[UUID] = Query(
        None,
        description="If set, reformat WHY only for this insight row.",
    ),
    limit: int = Query(
        2000,
        ge=1,
        le=10_000,
        description="Max rows when insight_id is not provided.",
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store=Depends(get_result_store),
):
    """Apply ``<sub>**…**<sub>`` subsection headers, bullet spacing, and ** emphasis via Cohere.

    The model should emit normal newline characters; any accidental backslash-n sequences are
    normalized to real newlines before validation and persistence. In this LLM-only pathway,
    normalized prose drift does not trigger deterministic fallback.
    """
    store = _select_main_insights_store(store, main_insights_table)
    logger.info(
        "POST /pbi/insights/main-insights/reformat-why-structure-sonnet run_timestamp=%s insight_id=%s limit=%s",
        run_timestamp,
        insight_id,
        limit,
    )
    try:
        engine = InsightEngine(
            store, main_insights_llm=MainInsightsNarrativeModel.cohere_command_a_plus
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        updated, ts_used = await engine.reformat_main_insight_why_structure_with_sonnet(
            run_timestamp=run_timestamp,
            insight_id=insight_id,
            limit=limit,
        )
        return WhySummaryResponse(
            message=f"Reformatted WHY structure on {updated} row(s)",
            updated=updated,
            run_timestamp=ts_used,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/main-insights/reformat-markup",
    response_model=ReformatMarkupResponse,
    summary="Reformat stored main insight markup (LLM by default)",
)
async def post_main_insights_reformat_markup(
    run_timestamp: Optional[datetime] = Query(
        None,
        description=(
            "If set, only rows with this run_timestamp. Ignored if insight_id is set. "
            "If both are omitted, processes up to **limit** rows (newest first)."
        ),
    ),
    insight_id: Optional[UUID] = Query(
        None,
        description="If set, only this row is reformatted.",
    ),
    limit: int = Query(
        2000,
        ge=1,
        le=10_000,
        description="Max rows when filtering by run_timestamp only or when no filter is set.",
    ),
    use_llm: bool = Query(
        True,
        description=(
            "If true (default), each narrative field is formatted by the narrative LLM with a strict "
            "markup-only prompt (falls back to rules if the model changes wording). "
            "If false, uses deterministic rules only (no extra API cost)."
        ),
    ),
    main_insights_llm: MainInsightsNarrativeModel = Query(
        MainInsightsNarrativeModel.azure_default,
        description=(
            "Used only when **use_llm** is true — same deployments as main-insight generation "
            "(e.g. **azure_gpt54_mini** for a smaller/faster model)."
        ),
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store=Depends(get_result_store),
):
    """Add **…** emphasis for metrics and business dimensions; title stays plain (asterisks stripped).

    Default path calls the configured chat model once per non-empty body field (insight, why, summaries,
    impact, recommended_actions). Set **use_llm=false** for regex-only formatting.
    """
    store = _select_main_insights_store(store, main_insights_table)
    logger.info(
        "POST /pbi/insights/main-insights/reformat-markup insight_id=%s run_timestamp=%s limit=%s use_llm=%s main_insights_llm=%s",
        insight_id,
        run_timestamp,
        limit,
        use_llm,
        main_insights_llm.value,
    )
    try:
        if use_llm:
            engine = InsightEngine(store, main_insights_llm=main_insights_llm)
        else:
            engine = InsightEngine(store)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        count = await engine.reformat_stored_main_insights_markup(
            insight_id=insight_id,
            run_timestamp=run_timestamp,
            limit=limit,
            use_llm=use_llm,
        )
        return ReformatMarkupResponse(
            message=f"Reformatted markup on {count} row(s)",
            updated=count,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put(
    "/main-insights/{insight_id}",
    response_model=dict[str, Any],
    summary="Update like / dislike / remarks / park on a main insight",
)
async def put_main_insight_feedback(
    insight_id: UUID,
    body: MainInsightFeedbackBody,
    pascal_case: bool = Query(
        True,
        description="If true (default), response keys match vw_main_insights (InsightID, …, Park, Like, Dislike, Remarks, RecommendedActions).",
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store=Depends(get_result_store),
):
    """Updates ``insights.main_insights`` for the given ``insight_id``.

    Send only the fields to change (partial update). For nullable columns (like, dislike, remarks),
    JSON ``null`` clears when the key is present. ``park`` is non-nullable in the DB (boolean).
    """
    store = _select_main_insights_store(store, main_insights_table)
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of: like, dislike, remarks, park (nullable fields may use null to clear).",
        )
    n = await store.update_main_insight_feedback(insight_id, updates)
    if n == 0:
        raise HTTPException(status_code=404, detail="main_insight not found or no matching row")
    row = await store.get_main_insight_by_id(insight_id, pascal_case=pascal_case)
    if row is None:
        raise HTTPException(status_code=404, detail="main_insight not found")
    return row


@router.post(
    "/run-pull-pipeline",
    response_model=InsightResponse,
    summary="Run pull pipeline (main insights + recommended actions)",
)
async def run_pull_pipeline(
    background_tasks: BackgroundTasks,
    run_timestamp: Optional[datetime] = Query(
        None,
        description=(
            "If **omitted**: loads **all** rows from signal_log, runs **one-shot standard main insights** "
            "(one LLM call per KPI × dimension), run_timestamp = max(detected_at) in that load, "
            "then fills **recommended_actions** (LLM) for those main insights. "
            "If **set**: runs the same one-shot standard main insights for that ``detected_at`` batch, "
            "then recommended actions."
        ),
    ),
    then_dimensional: bool = Query(
        False,
        description="After the pipeline step completes, run dimensional main insights in the **background**.",
    ),
    dimensional_all_timestamps: bool = Query(True, description="With **then_dimensional**."),
    target_dimensions: Optional[list[str]] = Query(
        None, description="With **then_dimensional**: optional dimension allow-list."
    ),
    main_insights_table: Optional[str] = Query(
        None,
        description=MAIN_INSIGHTS_TABLE_QUERY_HELP,
    ),
    store: ResultStore = Depends(get_result_store),
):
    """
    **Run pull pipeline** — with ``run_timestamp`` **unset**: one-shot **standard main insights** for **all**
    signals in ``signal_log``, then **recommended_actions** for that batch.

    With ``run_timestamp`` **set**: one-shot standard ``main_insights`` for that batch’s signals, then
    **recommended_actions**.
    """
    store = _select_main_insights_store(store, main_insights_table)
    logger.info(
        "POST /pbi/insights/run-pull-pipeline started run_timestamp=%s (access log after completion)",
        run_timestamp,
    )
    engine = InsightEngine(store)
    try:
        if run_timestamp is None:
            count, ts, skipped_alpha = await engine.generate_standard_main_insights(
                None, all_timestamps=True
            )
            if ts is None:
                logger.info("POST /pbi/insights/run-pull-pipeline: no signals; skip insights")
                return InsightResponse(
                    message="No signals in signal_log; nothing generated",
                    insights_generated=0,
                    recommended_actions_updated=0,
                    run_timestamp=None,
                    skipped_groups=[],
                )
            ra_n, _ = await engine.generate_recommended_actions(
                ts, None, skip_existing=False
            )
            if then_dimensional:
                background_tasks.add_task(
                    _background_dimensional_clustering,
                    store,
                    all_timestamps=dimensional_all_timestamps,
                    target_dimensions=target_dimensions,
                )
            logger.info(
                "POST /pbi/insights/run-pull-pipeline finished (standard one-shot) insights=%s recommended_actions=%s ts=%s then_dimensional=%s skipped=%s",
                count,
                ra_n,
                ts,
                then_dimensional,
                len(skipped_alpha),
            )
            msg_pipe = (
                "Standard main insights and recommended actions for all signals"
                + (
                    " — dimensional main insights scheduled in background (see [chained] in logs)"
                    if then_dimensional
                    else ""
                )
            )
            if skipped_alpha:
                msg_pipe += f" — {len(skipped_alpha)} group(s) skipped (see skipped_groups)"
            return InsightResponse(
                message=msg_pipe,
                insights_generated=count,
                recommended_actions_updated=ra_n,
                run_timestamp=ts,
                skipped_groups=skipped_alpha,
            )

        count = await engine.generate_main_insights(run_timestamp, cluster_type="alpha")
        ra_n, _ = await engine.generate_recommended_actions(
            run_timestamp, None, skip_existing=False
        )
        if then_dimensional:
            background_tasks.add_task(
                _background_dimensional_clustering,
                store,
                all_timestamps=dimensional_all_timestamps,
                target_dimensions=target_dimensions,
            )
        logger.info(
            "POST /pbi/insights/run-pull-pipeline finished insights=%s recommended_actions=%s then_dimensional=%s",
            count,
            ra_n,
            then_dimensional,
        )
        return InsightResponse(
            message=(
                "Main insights and recommended actions for this batch"
                + (
                    " — dimensional main insights scheduled in background"
                    if then_dimensional
                    else ""
                )
            ),
            insights_generated=count,
            recommended_actions_updated=ra_n,
            run_timestamp=run_timestamp,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
