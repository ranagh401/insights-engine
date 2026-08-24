"""Trigger endpoints — run signals and WHY analysis on demand.

POST /pbi/trigger/signals                    → full run (all active signal jobs)
POST /pbi/trigger/signals/{kpi}              → single-KPI run
POST /pbi/trigger/whys                       → WHY sweep (all unprocessed signals)
POST /pbi/trigger/whys/batch                 → WHY for an explicit list of signal IDs only
POST /pbi/trigger/whys/by-kpis               → WHY for all signals whose ``kpi_name`` matches a list
POST /pbi/trigger/whys/{signal_id}           → WHY for one signal
POST /pbi/trigger/full-insights-pipeline     → 4 steps (see handler docstrings)
POST /pbi/trigger/full-insights-pipeline/stream  → same pipeline with SSE progress
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..dependencies import (
    create_pipeline_feature_generator,
    get_config_loader,
    get_dax_settings,
    get_feature_generator,
    get_result_store,
    get_why_analyzer,
)
from ...config.config_loader import ConfigLoader
from ...config.models import SignalJobConfig
from ...engine.feature_generator import FeatureGenerator
from ...engine.signal_detector import SignalDetector
from ...engine.signal_dax_period import why_period_bounds_from_signal
from ...engine.why_analyzer import WhyAnalyzer
from ...store.result_store import ResultStore

logger = logging.getLogger(__name__)
router = APIRouter()


class TriggerSignalsRequest(BaseModel):
    job_ids: Optional[list[int]] = Field(
        None, description="Restrict to these job IDs (omit for all active jobs)"
    )
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    skip_why: bool = Field(False, description="Skip WHY analysis after signal detection")
    dimension_name: Optional[str] = Field(
        None,
        description=(
            "If set, only run expanded jobs whose analytical dimension matches (case-insensitive), "
            "e.g. ``Division``. Must match values in ``config_signaljobsclientportal.dimensions``."
        ),
    )
    exclude_kpi_names: Optional[list[str]] = Field(
        None,
        description="KPI names to skip (case-insensitive), e.g. [\"Cost per Demo\"].",
    )


class TriggerWhysRequest(BaseModel):
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class TriggerWhysBatchRequest(BaseModel):
    """Run WHY only for the listed ``signal_log`` rows (latest row per id)."""

    signal_ids: list[str] = Field(
        ...,
        min_length=1,
        description="One or more ``signal_id`` UUID strings from ``signal_log``.",
    )
    delete_existing_why_rows: bool = Field(
        True,
        description="If true, delete prior ``why_results`` for these signals before re-run (avoids duplicates).",
    )
    reset_why_computed: bool = Field(
        True,
        description="If true, set ``signal_log.why_computed=false`` before analysis (recommended).",
    )


class TriggerWhysByKpisRequest(BaseModel):
    """Resolve ``signal_id`` values from ``signal_log.kpi_name`` and run WHY (same engine as batch)."""

    kpi_names: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "One or more parent KPI names as stored on ``signal_log.kpi_name`` "
            "(case-insensitive), e.g. metrics whose ``configkpidependenciesclientportal`` rows you just updated."
        ),
    )
    delete_existing_why_rows: bool = Field(
        True,
        description="If true, delete prior ``why_results`` for resolved signals before re-run.",
    )
    reset_why_computed: bool = Field(
        True,
        description="If true, set ``signal_log.why_computed=false`` before analysis (recommended).",
    )
    only_unprocessed: bool = Field(
        False,
        description=(
            "If true, only rows with ``why_computed=false``. If false (default), every distinct "
            "signal for these KPIs is included so re-runs pick up newly configured dependency drivers."
        ),
    )


# ── Signal triggers ──────────────────────────────────────────────────────────

@router.post("/signals", response_model=dict[str, Any])
async def trigger_signals_full(
    body: TriggerSignalsRequest,
    background_tasks: BackgroundTasks,
    loader: ConfigLoader = Depends(get_config_loader),
    feat_gen: FeatureGenerator = Depends(get_feature_generator),
    store: ResultStore = Depends(get_result_store),
    analyzer: WhyAnalyzer = Depends(get_why_analyzer),
):
    """Run signal detection for all active jobs (or a filtered subset)."""
    background_tasks.add_task(
        _run_signal_pipeline,
        loader=loader,
        _feat_gen=feat_gen,
        store=store,
        analyzer=analyzer,
        kpi_name=None,
        job_ids=body.job_ids,
        start_date=body.start_date,
        end_date=body.end_date,
        skip_why=body.skip_why,
        dimension_name=body.dimension_name,
        exclude_kpi_names=body.exclude_kpi_names,
    )
    return {
        "status": "started",
        "scope": "full",
        "job_ids": body.job_ids,
        "dimension_name": body.dimension_name,
        "exclude_kpi_names": body.exclude_kpi_names,
    }


@router.post("/signals/{kpi_name}", response_model=dict[str, Any])
async def trigger_signals_for_kpi(
    kpi_name: str,
    body: TriggerSignalsRequest,
    background_tasks: BackgroundTasks,
    loader: ConfigLoader = Depends(get_config_loader),
    feat_gen: FeatureGenerator = Depends(get_feature_generator),
    store: ResultStore = Depends(get_result_store),
    analyzer: WhyAnalyzer = Depends(get_why_analyzer),
):
    """Run signal detection for a single KPI."""
    background_tasks.add_task(
        _run_signal_pipeline,
        loader=loader,
        _feat_gen=feat_gen,
        store=store,
        analyzer=analyzer,
        kpi_name=kpi_name,
        job_ids=body.job_ids,
        start_date=body.start_date,
        end_date=body.end_date,
        skip_why=body.skip_why,
        dimension_name=body.dimension_name,
        exclude_kpi_names=body.exclude_kpi_names,
    )
    return {
        "status": "started",
        "scope": kpi_name,
        "dimension_name": body.dimension_name,
        "exclude_kpi_names": body.exclude_kpi_names,
    }


@router.post("/run-all-signals", response_model=dict[str, Any])
async def trigger_all_signals_auto(
    background_tasks: BackgroundTasks,
    loader: ConfigLoader = Depends(get_config_loader),
    feat_gen: FeatureGenerator = Depends(get_feature_generator),
    store: ResultStore = Depends(get_result_store),
    analyzer: WhyAnalyzer = Depends(get_why_analyzer),
):
    """Run signal detection for all active jobs automatically (no body required)."""
    background_tasks.add_task(
        _run_signal_pipeline,
        loader=loader,
        _feat_gen=feat_gen,
        store=store,
        analyzer=analyzer,
        kpi_name=None,
        job_ids=None,
        start_date=None,
        end_date=None,
        skip_why=False,
        dimension_name=None,
        exclude_kpi_names=None,
    )
    return {"status": "started", "scope": "all_active_jobs"}


# ── WHY triggers ─────────────────────────────────────────────────────────────

@router.post("/whys", response_model=dict[str, Any])
async def trigger_whys_sweep(
    background_tasks: BackgroundTasks,
    analyzer: WhyAnalyzer = Depends(get_why_analyzer),
):
    """Run WHY analysis for ALL unprocessed signals."""
    background_tasks.add_task(_run_why_sweep, analyzer=analyzer)
    return {"status": "started", "scope": "all_unprocessed"}


@router.post("/whys/batch", response_model=dict[str, Any])
async def trigger_whys_batch(
    body: TriggerWhysBatchRequest,
    background_tasks: BackgroundTasks,
    analyzer: WhyAnalyzer = Depends(get_why_analyzer),
):
    """Run WHY analysis for **only** the given ``signal_ids`` (no full-queue sweep).

    Typical use: after fixing ``configkpidependenciesclientportal`` or thresholds, re-run WHY
    for a handful of signals without processing the entire unprocessed backlog.
    """
    background_tasks.add_task(
        _run_why_batch,
        analyzer=analyzer,
        signal_ids=body.signal_ids,
        delete_existing_why_rows=body.delete_existing_why_rows,
        reset_why_computed=body.reset_why_computed,
    )
    return {
        "status": "started",
        "scope": "signal_id_batch",
        "n_signal_ids": len(body.signal_ids),
        "delete_existing_why_rows": body.delete_existing_why_rows,
        "reset_why_computed": body.reset_why_computed,
    }


@router.post("/whys/by-kpis", response_model=dict[str, Any])
async def trigger_whys_by_kpis(
    body: TriggerWhysByKpisRequest,
    background_tasks: BackgroundTasks,
    store: ResultStore = Depends(get_result_store),
    analyzer: WhyAnalyzer = Depends(get_why_analyzer),
):
    """Run WHY for every distinct ``signal_id`` whose alert KPI matches ``kpi_names``.

    Typical use: after seeding or editing dependency rows, recompute drill-down whys for those
    parent metrics only, without a full-queue sweep.
    """
    signal_ids = await store.list_distinct_signal_ids_for_kpi_names(
        body.kpi_names,
        only_unprocessed=body.only_unprocessed,
    )
    if not signal_ids:
        raise HTTPException(
            status_code=404,
            detail=(
                "No signal_log rows matched the given KPI names with the current filter "
                "(check spelling; try only_unprocessed=false if the queue is empty)."
            ),
        )
    background_tasks.add_task(
        _run_why_batch,
        analyzer=analyzer,
        signal_ids=signal_ids,
        delete_existing_why_rows=body.delete_existing_why_rows,
        reset_why_computed=body.reset_why_computed,
    )
    return {
        "status": "started",
        "scope": "kpi_names",
        "kpi_names": body.kpi_names,
        "only_unprocessed": body.only_unprocessed,
        "n_signal_ids": len(signal_ids),
        "delete_existing_why_rows": body.delete_existing_why_rows,
        "reset_why_computed": body.reset_why_computed,
    }


class FullInsightsPipelineRequest(BaseModel):
    """Optional filters for the signal-detection leg of the full pipeline."""

    job_ids: Optional[list[int]] = Field(
        None, description="Restrict signal jobs to these IDs (omit for all active jobs)"
    )
    dimension_name: Optional[str] = Field(
        None,
        description="Step 1 (signals): only this analytical dimension, e.g. Division (case-insensitive).",
    )
    exclude_kpi_names: Optional[list[str]] = Field(
        None,
        description="Step 1 (signals): KPI names to skip, e.g. [\"Cost per Demo\"] (case-insensitive).",
    )
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    clustering_all_timestamps: bool = Field(
        False,
        description="If true, standard + dimensional insight steps load entire signal_log; default uses latest batch only.",
    )


PIPELINE_STEP_COUNT = 4


def _full_pipeline_step_labels() -> dict[str, str]:
    return {
        "signals": "Signal detection",
        "whys": "Why analysis",
        "clustering": "Main insights (standard + dimensional)",
        "main_insights": "Main insights (compat / no-op)",
    }


@router.post("/full-insights-pipeline", response_model=dict[str, Any])
async def trigger_full_insights_pipeline(
    body: FullInsightsPipelineRequest,
    background_tasks: BackgroundTasks,
    loader: ConfigLoader = Depends(get_config_loader),
    feat_gen: FeatureGenerator = Depends(get_feature_generator),
    store: ResultStore = Depends(get_result_store),
    analyzer: WhyAnalyzer = Depends(get_why_analyzer),
):
    """Run end-to-end: signal detection, WHY analysis, standard + dimensional main insights, and a no-op main_insights step.

    Runs in a **background task** (same pattern as ``/trigger/signals``). For live per-step status, use
    ``POST /pbi/trigger/full-insights-pipeline/stream`` (Server-Sent Events).
    """
    background_tasks.add_task(
        _run_full_insights_pipeline,
        loader=loader,
        feat_gen=feat_gen,
        store=store,
        analyzer=analyzer,
        job_ids=body.job_ids,
        start_date=body.start_date,
        end_date=body.end_date,
        clustering_all_timestamps=body.clustering_all_timestamps,
        dimension_name=body.dimension_name,
        exclude_kpi_names=body.exclude_kpi_names,
    )
    return {
        "status": "started",
        "message": "Queued full insights pipeline — use POST /pbi/trigger/full-insights-pipeline/stream for SSE, or see logs [pipeline] / [signals] / [why].",
        "steps": [
            "signals",
            "whys",
            "standard_and_dimensional_main_insights",
            "main_insights",
        ],
        "clustering_all_timestamps": body.clustering_all_timestamps,
        "dimension_name": body.dimension_name,
        "exclude_kpi_names": body.exclude_kpi_names,
    }


@router.post("/full-insights-pipeline/stream")
async def trigger_full_insights_pipeline_stream(
    body: FullInsightsPipelineRequest,
    loader: ConfigLoader = Depends(get_config_loader),
    feat_gen: FeatureGenerator = Depends(get_feature_generator),
    store: ResultStore = Depends(get_result_store),
    analyzer: WhyAnalyzer = Depends(get_why_analyzer),
):
    """Run the same four-step full pipeline, streaming JSON events as ``text/event-stream`` (SSE).

    Each line is a standard SSE data frame: ``data: {..json..}\\n\\n``.

    **Event types**

    * ``"step"`` — progress; fields include ``step`` (1–4), ``total_steps`` (4), ``status`` (``"started"`` or ``"completed"``), optional ``subphase`` for clustering (``"alpha"`` / ``"dimensional"``), and ``detail`` for summary metrics.
    * ``"complete"`` — pipeline finished (``"ok"``: true, ``"total_elapsed_s"``).
    * ``"error"`` — unrecoverable failure (``"message"``, optional ``"step"``).
    """
    async def _sse() -> AsyncIterator[bytes]:
        line: str
        try:
            async for event in _iter_full_insights_pipeline_events(
                loader=loader,
                feat_gen=feat_gen,
                store=store,
                analyzer=analyzer,
                job_ids=body.job_ids,
                start_date=body.start_date,
                end_date=body.end_date,
                clustering_all_timestamps=body.clustering_all_timestamps,
                dimension_name=body.dimension_name,
                exclude_kpi_names=body.exclude_kpi_names,
            ):
                line = f"data: {json.dumps(event, default=str)}\n\n"
                yield line.encode("utf-8")
        except Exception as e:
            err = {
                "event": "error",
                "ok": False,
                "message": str(e),
            }
            yield f"data: {json.dumps(err, default=str)}\n\n".encode("utf-8")

    return StreamingResponse(
        _sse(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/whys/{signal_id}", response_model=dict[str, Any])
async def trigger_why_single(
    signal_id: str,
    body: TriggerWhysRequest,
    background_tasks: BackgroundTasks,
    analyzer: WhyAnalyzer = Depends(get_why_analyzer),
    store: ResultStore = Depends(get_result_store),
):
    """Run WHY analysis for a single signal."""
    match = await store.get_unprocessed_signal_by_id(signal_id)
    if not match:
        existing = await store.get_why_results(signal_id)
        if existing:
            return {
                "status": "already_computed",
                "signal_id": signal_id,
                "why_rows": len(existing.why_rows),
            }
        raise HTTPException(404, f"Signal '{signal_id}' not found")

    background_tasks.add_task(
        _run_why_single,
        analyzer=analyzer,
        signal=match,
        start_date=body.start_date,
        end_date=body.end_date,
    )
    return {"status": "started", "signal_id": signal_id}


# ── Background task runners ──────────────────────────────────────────────────

def _est_seconds_per_signal_job() -> float:
    """Ballpark duration of one signal job (DAX-heavy); tune via env."""
    raw = os.environ.get("INSIGHTS_EST_SEC_PER_JOB", "").strip()
    if not raw:
        return 75.0
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 75.0


def _format_eta_seconds(sec: float) -> str:
    if sec < 0 or sec > 864000:
        return "?"
    if sec >= 120:
        return f"~{sec / 60:.1f}m"
    return f"~{sec:.0f}s"


def _signal_job_concurrency() -> int:
    """Parallel KPI jobs. DAX calls stay bounded by ``PBIClient``'s semaphore."""
    dax = get_dax_settings()
    if dax is None:
        return 1
    raw = os.environ.get("INSIGHTS_SIGNAL_JOB_CONCURRENCY", "").strip()
    if raw.isdigit():
        return max(1, int(raw))
    return max(1, min(4, int(dax.PBI_MAX_CONCURRENT_QUERIES)))


async def _run_signal_pipeline(
    *,
    loader: ConfigLoader,
    _feat_gen: FeatureGenerator,
    store: ResultStore,
    analyzer: WhyAnalyzer,
    kpi_name: str | None,
    job_ids: list[int] | None,
    start_date: date | None,
    end_date: date | None,
    skip_why: bool,
    dimension_name: str | None = None,
    exclude_kpi_names: list[str] | None = None,
) -> None:
    today = date.today()
    _global_start = start_date or (today - timedelta(days=today.weekday()))
    _global_end = end_date or (_global_start + timedelta(days=6))

    if get_dax_settings() is None:
        logger.error(
            "[signals] aborted: DAX / PBI settings not loaded — fix .env and restart."
        )
        return

    logger.info(
        "[signals] pipeline start | scope_kpi=%s job_ids=%s period=%s→%s skip_why=%s "
        "dimension_name=%s exclude_kpi_names=%s",
        kpi_name or "ALL",
        job_ids,
        _global_start,
        _global_end,
        skip_why,
        dimension_name,
        exclude_kpi_names,
    )

    try:
        jobs = await loader.get_signal_jobs(active_only=True)
    except Exception:
        logger.exception("[signals] failed to load signal jobs from config")
        return

    if kpi_name:
        jobs = [j for j in jobs if j.kpi_name == kpi_name]
    if job_ids:
        id_strs = {str(i) for i in job_ids}
        jobs = [j for j in jobs if j.job_id in id_strs]
    if exclude_kpi_names:
        ex = {x.strip().lower() for x in exclude_kpi_names if x and str(x).strip()}
        if ex:
            jobs = [j for j in jobs if j.kpi_name.strip().lower() not in ex]
    if dimension_name is not None and str(dimension_name).strip():
        dnorm = str(dimension_name).strip().lower()
        jobs = [
            j for j in jobs if (j.dimension_name or "").strip().lower() == dnorm
        ]

    if not jobs:
        logger.warning("[signals] no matching jobs — check filters / active flag.")
        return

    n_jobs = len(jobs)
    conc = _signal_job_concurrency()
    sec_per = _est_seconds_per_signal_job()
    # Wave model: jobs fill conc slots; each wave ~ sec_per wall seconds if jobs similar.
    rough_wall_sec = (n_jobs / max(conc, 1)) * sec_per
    logger.info(
        "[signals] queued %d job(s) | period window %s→%s | parallel_jobs=%d",
        n_jobs,
        _global_start,
        _global_end,
        conc,
    )
    logger.info(
        "[signals] planning_eta | order_of_magnitude_wall=%s | "
        "model=(%d jobs ÷ %d workers) × ~%.0fs/job | tune INSIGHTS_EST_SEC_PER_JOB | "
        "live_eta=est_remaining on each [signals] progress line",
        _format_eta_seconds(rough_wall_sec),
        n_jobs,
        conc,
        sec_per,
    )
    for j in jobs:
        logger.debug(
            "[signals] job_cfg id=%s kpi=%s dim=%s signals=%d",
            j.job_id,
            j.kpi_name,
            j.dimension_name,
            len(j.signals or []),
        )

    sem = asyncio.Semaphore(conc)
    pipeline_t0 = time.perf_counter()
    in_flight: dict[str, str] = {}
    flight_lock = asyncio.Lock()

    async def run_one(
        idx: int, job: SignalJobConfig
    ) -> tuple[int, SignalJobConfig, list]:
        """Returns (1-based index, job, detected signals)."""
        _job_start = start_date or job.period_start or _global_start
        _job_end = end_date or job.period_end or _global_end
        key = str(idx)
        label_short = f"id={job.job_id} kpi={job.kpi_name} dim={job.dimension_name}"
        run_label = f"job {idx}/{n_jobs} {label_short}"
        async with sem:
            async with flight_lock:
                in_flight[key] = label_short
                active_now = " | ".join(
                    in_flight[k] for k in sorted(in_flight.keys(), key=int)
                )
            logger.info(
                "[signals] job start %d/%d | %s | period=%s→%s | concurrent=[%s]",
                idx,
                n_jobs,
                label_short,
                _job_start,
                _job_end,
                active_now,
            )
            t0 = time.perf_counter()
            try:
                detector = SignalDetector(
                    config_loader=loader,
                    feature_generator=create_pipeline_feature_generator(
                        run_label=run_label,
                    ),
                    result_store=store,
                )
                signals = await detector.detect_signals(job, _job_start, _job_end)
            except Exception:
                logger.exception(
                    "[signals] job failed | index=%d/%d | job_id=%s | kpi=%s",
                    idx,
                    n_jobs,
                    job.job_id,
                    job.kpi_name,
                )
                async with flight_lock:
                    in_flight.pop(key, None)
                return (idx, job, [])
            elapsed = time.perf_counter() - t0
            async with flight_lock:
                in_flight.pop(key, None)
            logger.info(
                "[signals] job finish | index=%d/%d | job_id=%s | %.1fs | signals=%d",
                idx,
                n_jobs,
                job.job_id,
                elapsed,
                len(signals),
            )
            return (idx, job, signals)

    tasks = [run_one(idx, job) for idx, job in enumerate(jobs, 1)]
    outcomes: list[list] = []
    finished = 0
    running_signal_total = 0
    for fut in asyncio.as_completed(tasks):
        idx, job, signals = await fut
        outcomes.append(signals)
        finished += 1
        running_signal_total += len(signals)
        elapsed_wall = time.perf_counter() - pipeline_t0
        est_left = "?"
        if finished > 0 and elapsed_wall > 0.01:
            rate = finished / elapsed_wall
            est_left = _format_eta_seconds((n_jobs - finished) / rate)
        logger.info(
            "[signals] progress | jobs_completed=%d/%d | last_index=%d/%d | "
            "last_job_id=%s | kpi=%s | dim=%s | last_job_signals=%d | cumulative_signals=%d | "
            "elapsed=%s | est_remaining=%s",
            finished,
            n_jobs,
            idx,
            n_jobs,
            job.job_id,
            job.kpi_name,
            job.dimension_name,
            len(signals),
            running_signal_total,
            _format_eta_seconds(elapsed_wall),
            est_left,
        )
    total_signals = sum(len(s) for s in outcomes)
    pipeline_elapsed = time.perf_counter() - pipeline_t0
    logger.info(
        "[signals] detection complete | jobs=%d total_signals=%d wall_time=%.1fs",
        n_jobs,
        total_signals,
        pipeline_elapsed,
    )

    if not skip_why:
        logger.info(
            "[signals] WHY phase | running analyze_unprocessed() — "
            "per-signal progress uses the [why] log prefix"
        )
        try:
            t_why = time.perf_counter()
            n_why = await analyzer.analyze_unprocessed()
            logger.info(
                "[signals] WHY phase done | signals_processed=%d | wall_time=%.1fs",
                n_why,
                time.perf_counter() - t_why,
            )
        except Exception:
            logger.exception("[signals] WHY phase failed")


async def _run_why_with_dates(
    analyzer: WhyAnalyzer,
    period_start: date,
    period_end: date,
) -> list:
    """Run WHY analysis for unprocessed signals with explicit dates (DB-chunked)."""
    from ...engine.why_analyzer import _why_store_batch_sizes

    db_batch, _ = _why_store_batch_sizes()
    results = []
    chunk_i = 0
    total = 0

    while True:
        unprocessed = await analyzer._store.get_unprocessed_signals(
            limit=db_batch,
            order_detected_at="asc",
        )
        if not unprocessed:
            break
        chunk_i += 1
        n = len(unprocessed)
        logger.info(
            "[why] dated sweep chunk=%d | signals=%d | period=%s→%s | cumulative=%d",
            chunk_i,
            n,
            period_start,
            period_end,
            total,
        )
        for i, sig in enumerate(unprocessed, 1):
            job = SignalJobConfig(
                job_id=sig.job_id,
                kpi_name=sig.kpi_name,
                dimension_name=sig.dimension,
            )
            global_i = total + i
            logger.info(
                "[why] signal chunk=%d item=%d/%d | global~=%d | signal_id=%s | kpi=%s | dim=%s",
                chunk_i,
                i,
                n,
                global_i,
                sig.signal_id,
                sig.kpi_name,
                sig.dimension,
            )
            try:
                r = await analyzer.analyze_signal(sig, job, period_start, period_end)
                results.append(r)
                logger.info(
                    "[why] signal done | signal_id=%s | why_rows=%d",
                    sig.signal_id,
                    len(r.why_rows),
                )
            except Exception:
                logger.exception(
                    "[why] signal failed | signal_id=%s",
                    sig.signal_id,
                )
        total += n

    logger.info("[why] dated sweep complete | processed=%d", len(results))
    return results


async def _run_why_sweep(*, analyzer: WhyAnalyzer) -> None:
    logger.info("[why] API sweep task started | POST /pbi/trigger/whys")
    try:
        n_done = await analyzer.analyze_unprocessed()
        logger.info(
            "[why] API sweep task finished | signals_processed=%d",
            n_done,
        )
    except Exception:
        logger.exception("[why] API sweep task failed")


async def _run_why_batch(
    *,
    analyzer: WhyAnalyzer,
    signal_ids: list[str],
    delete_existing_why_rows: bool,
    reset_why_computed: bool,
) -> None:
    logger.info(
        "[why] API batch task started | POST /pbi/trigger/whys/batch | n_ids=%d",
        len(signal_ids),
    )
    try:
        out = await analyzer.analyze_signal_ids(
            signal_ids,
            delete_existing_why_rows=delete_existing_why_rows,
            reset_why_computed=reset_why_computed,
        )
        logger.info(
            "[why] API batch task finished | processed=%s why_rows=%s missing=%d",
            out.get("signals_processed"),
            out.get("why_rows_written"),
            len(out.get("missing_signal_ids") or []),
        )
    except Exception:
        logger.exception("[why] API batch task failed")


async def _iter_full_insights_pipeline_events(
    *,
    loader: ConfigLoader,
    feat_gen: FeatureGenerator,
    store: ResultStore,
    analyzer: WhyAnalyzer,
    job_ids: list[int] | None,
    start_date: date | None,
    end_date: date | None,
    clustering_all_timestamps: bool,
    dimension_name: str | None = None,
    exclude_kpi_names: list[str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yields progress dicts; caller runs the loop (background task or SSE)."""
    from ...engine.insight_engine import InsightEngine

    labels = _full_pipeline_step_labels()
    t0 = time.perf_counter()
    total = PIPELINE_STEP_COUNT

    def _elapsed() -> float:
        return round(time.perf_counter() - t0, 2)

    def _step(
        step: int,
        name: str,
        status: str,
        *,
        subphase: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "event": "step",
            "step": step,
            "total_steps": total,
            "name": name,
            "label": labels.get(name, name),
            "status": status,
            "elapsed_s": _elapsed(),
        }
        if subphase is not None:
            out["subphase"] = subphase
        if detail is not None:
            out["detail"] = detail
        return out

    logger.info(
        "[pipeline] start | clustering_all_timestamps=%s",
        clustering_all_timestamps,
    )

    # 1 — signals (inline WHY off; whys are step 2)
    yield _step(1, "signals", "started")
    await _run_signal_pipeline(
        loader=loader,
        _feat_gen=feat_gen,
        store=store,
        analyzer=analyzer,
        kpi_name=None,
        job_ids=job_ids,
        start_date=start_date,
        end_date=end_date,
        skip_why=True,
        dimension_name=dimension_name,
        exclude_kpi_names=exclude_kpi_names,
    )
    yield _step(1, "signals", "completed")

    # 2 — whys
    yield _step(2, "whys", "started")
    await _run_why_sweep(analyzer=analyzer)
    yield _step(2, "whys", "completed")

    engine = InsightEngine(store)

    # 3a — standard one-shot main insights (KPI × dimension; no cluster tables)
    yield _step(3, "clustering", "started", subphase="alpha")
    n_main_std, ts, skipped_alpha = await engine.generate_standard_main_insights(
        None, clustering_all_timestamps
    )
    yield _step(
        3,
        "clustering",
        "completed",
        subphase="alpha",
        detail={
            "main_insights_written": n_main_std,
            "run_timestamp": ts.isoformat() if ts is not None else None,
            "skipped_groups": skipped_alpha,
            "skipped_groups_count": len(skipped_alpha),
        },
    )

    # 3b — dimensional clustering
    yield _step(3, "clustering", "started", subphase="dimensional")
    d_n, d_ts, skipped_dim = await engine.cluster_signals_dimensional(
        None, None, clustering_all_timestamps
    )
    yield _step(
        3,
        "clustering",
        "completed",
        subphase="dimensional",
        detail={
            "dimensional_main_insights_written": d_n,
            "run_timestamp": d_ts.isoformat() if d_ts is not None else None,
            "skipped_groups": skipped_dim,
            "skipped_groups_count": len(skipped_dim),
        },
    )

    # 4 — main insights (already generated in 3a for standard path; kept for step compatibility)
    yield _step(4, "main_insights", "started")
    yield _step(
        4,
        "main_insights",
        "completed",
        detail={"main_insight_rows": n_main_std, "note": "same batch as step 3a standard one-shot"},
    )

    logger.info("[pipeline] finished successfully | elapsed_s=%.2f", _elapsed())
    yield {
        "event": "complete",
        "ok": True,
        "total_steps": total,
        "total_elapsed_s": _elapsed(),
    }


async def _run_why_single(
    *, analyzer: WhyAnalyzer, signal, start_date, end_date
) -> None:
    jobs = await analyzer._loader.get_signal_jobs(active_only=False)
    job = next((j for j in jobs if j.job_id == signal.job_id), None)
    
    if not job:
        job = SignalJobConfig(
            job_id=signal.job_id,
            kpi_name=signal.kpi_name,
            dimension_name=signal.dimension,
        )

    today = date.today()
    if job.period_start is not None and job.period_end is not None:
        _start = start_date or job.period_start
        _end = end_date or job.period_end
    else:
        dax_ps, dax_pe = why_period_bounds_from_signal(signal)
        if dax_ps is not None and dax_pe is not None:
            _start = start_date or dax_ps
            _end = end_date or dax_pe
        else:
            _start = start_date or job.period_start or today
            _end = end_date or job.period_end or today

    logger.info(
        "[why] single task start | POST /pbi/trigger/whys/{id} | signal_id=%s | job_id=%s | kpi=%s | dim=%s",
        signal.signal_id,
        signal.job_id,
        signal.kpi_name,
        signal.dimension,
    )
    try:
        t0 = time.perf_counter()
        result = await analyzer.analyze_signal(signal, job, _start, _end)
        logger.info(
            "[why] single task done | signal_id=%s | why_rows=%d | %.1fs",
            signal.signal_id,
            len(result.why_rows),
            time.perf_counter() - t0,
        )
    except Exception:
        logger.exception("[why] single task failed | signal_id=%s", signal.signal_id)


async def _run_full_insights_pipeline(
    *,
    loader: ConfigLoader,
    feat_gen: FeatureGenerator,
    store: ResultStore,
    analyzer: WhyAnalyzer,
    job_ids: list[int] | None,
    start_date: date | None,
    end_date: date | None,
    clustering_all_timestamps: bool,
    dimension_name: str | None = None,
    exclude_kpi_names: list[str] | None = None,
) -> None:
    """Sequential four-step pipeline; progress also available via _iter_full_insights_pipeline_events / SSE stream."""
    try:
        async for _ in _iter_full_insights_pipeline_events(
            loader=loader,
            feat_gen=feat_gen,
            store=store,
            analyzer=analyzer,
            job_ids=job_ids,
            start_date=start_date,
            end_date=end_date,
            clustering_all_timestamps=clustering_all_timestamps,
            dimension_name=dimension_name,
            exclude_kpi_names=exclude_kpi_names,
        ):
            pass
    except Exception:
        logger.exception("[pipeline] failed")
