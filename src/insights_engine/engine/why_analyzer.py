"""WHY analyzer — for each breaching signal, drill into cross-dimensions
and dependent KPIs to find **which sub-entities also breach the same
signal threshold**.

Route A (cross-dimension):
    For each valid dimension (except the signal's own), execute current +
    prior-period DAX queries, compute the same feature, apply the same
    threshold, and keep only breaching rows.

Route B (dependency KPI):
    For each dependent KPI, execute current + prior-period DAX queries
    on the signal's own dimension, compute the same feature, apply the
    same threshold, and keep only breaching rows.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import uuid4

import pandas as pd

from ..config.config_loader import ConfigLoader
from ..config.models import (
    DimensionRef,
    Signal,
    SignalJobConfig,
    WhyAnalysisResult,
    WhyRow,
    resolve_job_filter_dimension,
)
from ..dax.query_builder import DAXQueryBuilder, build_union_why_query
from ..engine.feature_generator import (
    FeatureGenerator,
    _apply_filters,
    _compute_feature_from_periods,
    _prior_period_dates,
)
from ..engine.metric_display import kpi_uses_percentage_point_difference
from ..store.result_store import ResultStore

from .signal_dax_period import why_period_bounds_from_signal

logger = logging.getLogger(__name__)


def _why_change_magnitude_phrase(kpi_for_change: str, change_val: float | None) -> str:
    """Human label for WHY row change: percentage points for rate KPIs, else percent."""
    if change_val is None:
        return ""
    if kpi_uses_percentage_point_difference(kpi_for_change):
        return f"{abs(change_val):.1f} percentage points"
    return f"{abs(change_val):.1f}%"


_WHY_CONCURRENCY_DEFAULT = 16
_WHY_GROUP_CONCURRENCY_DEFAULT = 4


def _why_store_batch_sizes() -> tuple[int, int]:
    """(db_fetch_chunk, cache_entity_subbatch) from Settings / safe defaults."""
    from ..settings import get_dax_settings

    try:
        s = get_dax_settings()
        db_b = int(getattr(s, "INSIGHTS_WHY_DB_FETCH_BATCH", 5000) or 5000)
        ent_b = int(getattr(s, "INSIGHTS_WHY_CACHE_ENTITY_BATCH", 400) or 400)
    except Exception:
        db_b, ent_b = 5000, 400
    return max(50, db_b), max(20, ent_b)


def _why_concurrency() -> int:
    """Number of parallel signal WHY tasks within a cache group."""
    raw = os.environ.get("INSIGHTS_WHY_CONCURRENCY", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _WHY_CONCURRENCY_DEFAULT


def _why_group_concurrency() -> int:
    """Number of (kpi, dim, feature) groups processed in parallel."""
    raw = os.environ.get("INSIGHTS_WHY_GROUP_CONCURRENCY", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _WHY_GROUP_CONCURRENCY_DEFAULT


def _format_eta(seconds: float) -> str:
    if seconds < 0 or seconds > 864_000:
        return "?"
    if seconds >= 120:
        return f"~{seconds / 60:.1f}m"
    return f"~{seconds:.0f}s"


_OPS = {
    "lt": lambda v, t, _t2=None: v < t,
    "gt": lambda v, t, _t2=None: v > t,
    "lte": lambda v, t, _t2=None: v <= t,
    "gte": lambda v, t, _t2=None: v >= t,
    "between": lambda v, t1, t2: t1 <= v <= t2 if t2 is not None else False,
}

@dataclass
class WhyDataCache:
    """Holds bulk data for all evaluated entities to eliminate N+1 querying."""
    route_a_current: dict[str, pd.DataFrame]
    route_a_prior: dict[tuple[str, str], pd.DataFrame]  
    route_b_current: dict[str, pd.DataFrame]
    route_b_prior: dict[tuple[str, str], pd.DataFrame]


def _union_why_col_key(name: object) -> str:
    """Strip noise from PBI ExecuteQueries column headers for fuzzy matching."""
    return re.sub(r"[^a-z0-9]+", "", str(name).casefold())


def _normalize_union_why_result_df(df: pd.DataFrame) -> pd.DataFrame:
    """Map PBI JSON column names to the PascalCase aliases ``_defragment_union_result`` expects.

    The REST API often returns ``[DimensionName]``, different casing, or prefixed names;
    without this step ``DimensionName`` is missing and the consolidated cache is empty.
    """
    if df.empty:
        return df
    slug_to_std: tuple[tuple[str, str], ...] = (
        ("dimensionname", "DimensionName"),
        ("signaldimvalue", "SignalDimValue"),
        ("dimensionvalue", "DimensionValue"),
        ("kpivalue", "KPIValue"),
    )
    renames: dict[str, str] = {}
    used_std: set[str] = set()
    for col in df.columns:
        k = _union_why_col_key(col)
        for slug, std in slug_to_std:
            if std in used_std:
                continue
            if k == slug or k.endswith(slug):
                renames[col] = std
                used_std.add(std)
                break
    if not renames:
        return df
    out = df.rename(columns=renames)
    return out


def _why_cache_has_any_route_frames(cache: WhyDataCache) -> bool:
    for d in cache.route_a_current.values():
        if isinstance(d, pd.DataFrame) and not d.empty:
            return True
    for d in cache.route_b_current.values():
        if isinstance(d, pd.DataFrame) and not d.empty:
            return True
    return False


class WhyAnalyzer:
    """Computes WHY analysis for breaching signals."""

    def __init__(
        self,
        kpi_engine,
        config_loader: ConfigLoader,
        result_store: ResultStore,
        feature_generator: FeatureGenerator | None = None,
    ) -> None:
        self._engine = kpi_engine
        self._loader = config_loader
        self._store = result_store
        self._feat_gen = feature_generator

    def _get_feat_gen(self) -> FeatureGenerator:
        if self._feat_gen is not None:
            return self._feat_gen
        return FeatureGenerator(
            config_loader=self._loader,
            pbi_client=self._engine._pbi,
            settings=self._engine._settings,
        )

    async def analyze_unprocessed(self) -> int:
        """Process signals with ``why_computed = FALSE`` until the queue is empty.

        Mirrors the Databricks ``SignalEngine.compute_whys`` pattern:
         * DB chunks (``INSIGHTS_WHY_DB_FETCH_BATCH``) keep RAM bounded.
         * Entity sub-batches (``INSIGHTS_WHY_CACHE_ENTITY_BATCH``) keep DAX
           member-filter size safe.
         * **Parallel workers** (``INSIGHTS_WHY_CONCURRENCY``, default 16)
           process signals concurrently within each cache group — the main
           throughput lever, analogous to ``ThreadPoolExecutor(max_workers=N)``
           in the ADB path.
         * Bulk DB writes: batched INSERT for why_results + batched UPDATE
           for ``mark_why_computed`` (one round-trip per flush, not per signal).
         * Signal-definition lookups cached per sweep to avoid repeated DB hits.

        **Note:** Rows are inserted into ``why_results`` only when Route A / Route B
        find **breaching** drill-down lines. If every signal yields zero such lines,
        the sweep still marks ``why_computed=true`` but the table stays empty — that
        is expected for this engine.

        Returns the number of signals fully processed.
        """
        db_batch, entity_batch = _why_store_batch_sizes()
        conc = _why_concurrency()
        try:
            pending = await self._store.count_unprocessed_signals()
        except Exception:
            pending = -1

        all_jobs = await self._loader.get_signal_jobs(active_only=False)
        job_map = {j.job_id: j for j in all_jobs}
        feat_gen: FeatureGenerator | None = None
        total_done = 0
        total_why_rows_written = 0
        sweep_t0 = time.perf_counter()

        group_conc = _why_group_concurrency()
        logger.info(
            "[why] sweep start | pending=%s | db_fetch_batch=%d | entity_batch=%d "
            "| signal_concurrency=%d | group_concurrency=%d",
            pending if pending >= 0 else "?",
            db_batch,
            entity_batch,
            conc,
            group_conc,
        )

        sig_def_cache: dict[str, object] = {}

        while True:
            chunk = await self._store.get_unprocessed_signals(
                limit=db_batch,
                order_detected_at="asc",
            )
            if not chunk:
                if total_done == 0:
                    logger.info("[why] sweep | no unprocessed signals (why_computed=false).")
                break

            elapsed = time.perf_counter() - sweep_t0
            rate = total_done / elapsed if elapsed > 1 else 0
            remaining = (pending - total_done) if pending >= 0 else -1
            eta_s = remaining / rate if rate > 0 and remaining > 0 else -1
            logger.info(
                "[why] db_chunk | loaded=%d | done=%d | pending~=%s | rate=%.1f sig/s | eta=%s",
                len(chunk),
                total_done,
                str(max(0, remaining)) if remaining >= 0 else "?",
                rate,
                _format_eta(eta_s) if eta_s > 0 else "?",
            )

            if feat_gen is None:
                feat_gen = self._get_feat_gen()

            n, n_why = await self._process_why_db_chunk(
                chunk,
                job_map,
                feat_gen,
                entity_batch,
                pending,
                total_done,
                conc,
                sig_def_cache,
                sweep_t0,
                group_conc,
            )
            total_done += n
            total_why_rows_written += n_why

        wall = time.perf_counter() - sweep_t0
        logger.info(
            "[why] sweep complete | processed=%d | why_results_rows_written=%d | "
            "wall_time=%.1fs | avg=%.2f sig/s",
            total_done,
            total_why_rows_written,
            wall,
            total_done / wall if wall > 0.1 else 0,
        )
        if total_done > 0 and total_why_rows_written == 0:
            logger.warning(
                "[why] no rows inserted into why_results — drill-down found no "
                "breaching Route A / Route B lines for these signals (signals were "
                "still marked why_computed=true). Check thresholds, cache data, or KPI config."
            )
        return total_done

    async def analyze_signal_ids(
        self,
        signal_ids: list[str],
        *,
        delete_existing_why_rows: bool = True,
        reset_why_computed: bool = True,
    ) -> dict[str, Any]:
        """Run WHY for an explicit list of ``signal_log.signal_id`` values only (no full sweep).

        Deletes prior ``why_results`` for those IDs when ``delete_existing_why_rows`` is true
        so re-runs do not duplicate rows. Uses the same grouped DAX cache path as the bulk
        sweep (``_process_why_db_chunk``).
        """
        uniq: list[str] = []
        seen: set[str] = set()
        for raw in signal_ids or []:
            s = str(raw).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            uniq.append(s)
        if not uniq:
            return {
                "signals_requested": 0,
                "signals_processed": 0,
                "why_rows_written": 0,
                "missing_signal_ids": [],
            }

        if delete_existing_why_rows:
            await self._store.delete_why_results_for_signal_ids(uniq)
        if reset_why_computed:
            await self._store.reset_why_computed_for_signal_ids(uniq)

        signals = await self._store.get_signals_latest_row_per_signal_ids(uniq)
        found = {str(s.signal_id) for s in signals}
        missing = [x for x in uniq if x not in found]

        if not signals:
            logger.warning("[why] batch | no signal_log rows for ids=%s", uniq[:20])
            return {
                "signals_requested": len(uniq),
                "signals_processed": 0,
                "why_rows_written": 0,
                "missing_signal_ids": missing,
            }

        all_jobs = await self._loader.get_signal_jobs(active_only=False)
        job_map = {j.job_id: j for j in all_jobs}
        feat_gen = self._get_feat_gen()
        db_batch, entity_batch = _why_store_batch_sizes()
        conc = _why_concurrency()
        group_conc = _why_group_concurrency()
        sweep_t0 = time.perf_counter()

        n_done, n_why = await self._process_why_db_chunk(
            signals,
            job_map,
            feat_gen,
            entity_batch,
            len(signals),
            0,
            conc,
            {},
            sweep_t0,
            group_conc,
        )
        logger.info(
            "[why] batch | requested=%d processed=%d why_rows=%d missing=%d",
            len(uniq),
            n_done,
            n_why,
            len(missing),
        )
        return {
            "signals_requested": len(uniq),
            "signals_processed": n_done,
            "why_rows_written": n_why,
            "missing_signal_ids": missing,
        }

    async def _process_why_db_chunk(
        self,
        chunk: list[Signal],
        job_map: dict[str, SignalJobConfig],
        feat_gen: FeatureGenerator,
        entity_batch: int,
        pending: int,
        done_offset: int,
        concurrency: int,
        sig_def_cache: dict[str, object],
        sweep_t0: float,
        group_concurrency: int = 4,
    ) -> tuple[int, int]:
        """Group one DB chunk → build caches + analyse signals **in parallel groups**.

        Returns ``(signals_processed_in_chunk, why_result_rows_flushed)``.
        """
        _Key = tuple[str, str, str, date, date]
        groups: dict[_Key, list[Signal]] = defaultdict(list)
        today = date.today()

        for sig in chunk:
            job = job_map.get((sig.job_id or "").strip())
            # Authoritative: ``config_signaljobsclientportal`` → ``SignalJobConfig.period_*`` (filters.period).
            if job is not None and job.period_start is not None and job.period_end is not None:
                p_start, p_end = job.period_start, job.period_end
            else:
                dax_ps, dax_pe = why_period_bounds_from_signal(sig)
                if dax_ps is not None and dax_pe is not None:
                    p_start, p_end = dax_ps, dax_pe
                elif job is not None:
                    p_start = job.period_start or today
                    p_end = job.period_end or today
                else:
                    p_start = today
                    p_end = today
            groups[(sig.kpi_name, sig.dimension, sig.feature_name, p_start, p_end)].append(sig)

        n_batches = len(groups)
        now = datetime.now(timezone.utc)
        sig_sem = asyncio.Semaphore(concurrency)
        group_sem = asyncio.Semaphore(group_concurrency)
        _FLUSH_EVERY = 500

        _lock = asyncio.Lock()
        all_why_rows: list[WhyRow] = []
        done_signal_ids: list[str] = []
        sig_done = 0
        why_rows_flushed = 0

        async def _flush():
            nonlocal all_why_rows, done_signal_ids, why_rows_flushed
            if all_why_rows:
                nbuf = len(all_why_rows)
                await self._store.write_why_results(all_why_rows)
                why_rows_flushed += nbuf
                all_why_rows = []
            if done_signal_ids:
                await self._store.mark_why_computed_bulk(done_signal_ids)
                done_signal_ids = []

        async def _process_group(
            batch_idx: int,
            kpi_name: str,
            dim_name: str,
            feat_name: str,
            period_start: date,
            period_end: date,
            signals: list[Signal],
        ) -> int:
            nonlocal sig_done, all_why_rows, done_signal_ids
            async with group_sem:
                group_done = 0
                try:
                    logger.info(
                        "[why] batch %d/%d | kpi=%s | dim=%s | feature=%s | period=%s→%s | "
                        "signals=%d | conc=%d | grp_conc=%d",
                        batch_idx, n_batches, kpi_name, dim_name, feat_name,
                        period_start, period_end, len(signals),
                        concurrency, group_concurrency,
                    )
                    kpi_config = await self._loader.get_kpi_config(kpi_name)
                    job = job_map.get(signals[0].job_id)
                    if not job:
                        job = SignalJobConfig(
                            job_id=signals[0].job_id,
                            kpi_name=kpi_name,
                            dimension_name=dim_name,
                        )

                    for sub_start in range(0, len(signals), entity_batch):
                        sub = signals[sub_start : sub_start + entity_batch]
                        cache = await self._build_why_cache(
                            kpi_config, dim_name, feat_name,
                            period_start, period_end, job, sub, feat_gen,
                        )

                        async def _do_one(sig: Signal, _cache=cache, _kpi=kpi_config,
                                          _fn=feat_name, _ps=period_start, _pe=period_end) -> tuple[str, list[WhyRow]]:
                            async with sig_sem:
                                rows = await self._analyze_signal_pure(
                                    sig, _kpi, _cache, _fn, _ps, _pe, now, sig_def_cache,
                                )
                                return sig.signal_id, rows

                        tasks = [asyncio.ensure_future(_do_one(s)) for s in sub]
                        results = await asyncio.gather(*tasks, return_exceptions=True)

                        async with _lock:
                            for i, res in enumerate(results):
                                sig_done += 1
                                group_done += 1
                                global_idx = done_offset + sig_done
                                sig = sub[i]
                                if isinstance(res, BaseException):
                                    logger.exception(
                                        "[why] signal failed | %d | signal_id=%s | %s",
                                        global_idx, sig.signal_id, res,
                                    )
                                    continue
                                sid, why_rows = res
                                all_why_rows.extend(why_rows)
                                done_signal_ids.append(sid)

                                if sig_done % 200 == 0:
                                    elapsed = time.perf_counter() - sweep_t0
                                    rate = (done_offset + sig_done) / elapsed if elapsed > 1 else 0
                                    remain = (pending - done_offset - sig_done) if pending >= 0 else -1
                                    eta_s = remain / rate if rate > 0 and remain > 0 else -1
                                    logger.info(
                                        "[why] progress | %d/%s | rate=%.1f sig/s | eta=%s | buffered_why_rows=%d",
                                        done_offset + sig_done,
                                        str(pending) if pending >= 0 else "?",
                                        rate,
                                        _format_eta(eta_s) if eta_s > 0 else "?",
                                        len(all_why_rows),
                                    )

                            if len(done_signal_ids) >= _FLUSH_EVERY:
                                await _flush()

                    logger.info(
                        "[why] batch %d/%d done | kpi=%s | dim=%s | group_signals=%d | total_processed=%d",
                        batch_idx, n_batches, kpi_name, dim_name, group_done, sig_done,
                    )
                except Exception:
                    logger.exception(
                        "[why] batch failed | batch=%d/%d | kpi=%s | dim=%s",
                        batch_idx, n_batches, kpi_name, dim_name,
                    )
                return group_done

        group_tasks = []
        for batch_idx, ((kpi_name, dim_name, feat_name, period_start, period_end), signals) in enumerate(
            groups.items(), 1
        ):
            group_tasks.append(
                asyncio.ensure_future(
                    _process_group(batch_idx, kpi_name, dim_name, feat_name,
                                   period_start, period_end, signals)
                )
            )

        await asyncio.gather(*group_tasks, return_exceptions=True)

        async with _lock:
            await _flush()
        return sig_done, why_rows_flushed

    async def analyze_signal(
        self,
        signal: Signal,
        job: SignalJobConfig,
        period_start: date,
        period_end: date,
    ) -> WhyAnalysisResult:
        """Run full WHY analysis for a single signal."""
        kpi_config = await self._loader.get_kpi_config(signal.kpi_name)
        feat_gen = self._get_feat_gen()
        now = datetime.now(timezone.utc)
        
        cache = await self._build_why_cache(
            kpi_config, signal.dimension, signal.feature_name, 
            period_start, period_end, job, [signal], feat_gen
        )
        return await self._analyze_signal_with_cache(
            signal, job, period_start, period_end, cache, kpi_config, feat_gen, now
        )

    async def _analyze_signal_pure(
        self,
        signal: Signal,
        kpi_config,
        cache: WhyDataCache,
        feat_name: str,
        period_start: date,
        period_end: date,
        now: datetime,
        sig_def_cache: dict[str, object] | None = None,
    ) -> list[WhyRow]:
        """Compute WHY rows for one signal — pure logic, **no** DB writes.

        The caller is responsible for batching ``write_why_results`` and
        ``mark_why_computed_bulk`` to amortise Postgres round-trips (same
        pattern as ``_flush_whys`` in the Databricks ``compute_whys`` path).
        """
        sig_name = signal.signal_name
        threshold = signal.threshold_value
        operator = signal.operator
        kpi_format = kpi_config.kpi_format
        period_str = f"{period_start} to {period_end}"

        threshold2 = None
        if operator == "between":
            if sig_def_cache is not None and sig_name in sig_def_cache:
                threshold2 = sig_def_cache[sig_name]
            else:
                try:
                    sig_defs = await self._loader.get_signal_definitions([sig_name])
                    if sig_defs:
                        threshold2 = sig_defs[0].threshold2
                    if sig_def_cache is not None:
                        sig_def_cache[sig_name] = threshold2
                except Exception:
                    pass

        why_rows: list[WhyRow] = []

        route_a_rows = await self._route_a_cross_dimension(
            signal, kpi_config, cache, feat_name, sig_name, threshold, threshold2,
            operator, kpi_format, period_start, period_end, period_str, now
        )
        why_rows.extend(route_a_rows)

        route_b_rows = await self._route_b_dependencies(
            signal, kpi_config, cache, feat_name, sig_name, threshold, threshold2,
            operator, kpi_format, period_start, period_end, period_str, now
        )
        why_rows.extend(route_b_rows)

        logger.debug(
            "WHY signal %s | route_a=%d route_b=%d",
            signal.signal_id, len(route_a_rows), len(route_b_rows),
        )
        return why_rows

    async def _analyze_signal_with_cache(
        self,
        signal: Signal,
        job: SignalJobConfig,
        period_start: date,
        period_end: date,
        cache: WhyDataCache,
        kpi_config,
        feat_gen: FeatureGenerator,
        now: datetime
    ) -> WhyAnalysisResult:
        """Legacy entry-point used by ``analyze_signal`` (single-signal API).

        Writes results + marks ``why_computed`` immediately (not batched).
        """
        why_rows = await self._analyze_signal_pure(
            signal, kpi_config, cache, signal.feature_name,
            period_start, period_end, now,
        )

        await self._store.write_why_results(why_rows)
        await self._store.mark_why_computed(signal.signal_id)

        result = WhyAnalysisResult(
            signal=signal,
            why_rows=why_rows,
            analysis_timestamp=now,
        )
        logger.info(
            "WHY analysis complete for signal %s  (why_rows=%d)",
            signal.signal_id, len(why_rows),
        )
        return result

    # ── Bulk Data Fetching ──────────────────────────────────────────────────

    async def _build_why_cache(
        self,
        kpi_config,
        dim_name: str,
        feat_name: str,
        period_start: date,
        period_end: date,
        job: SignalJobConfig,
        signals: list[Signal],
        feat_gen: FeatureGenerator,
    ) -> WhyDataCache:
        cache = WhyDataCache({}, {}, {}, {})
        signal_dim_ref = self._find_dim_ref(kpi_config, dim_name)
        if not signal_dim_ref:
            return cache

        unique_entities = list({s.dimension_value for s in signals if s.dimension_value and str(s.dimension_value).strip().lower() != "nan"})
        if not unique_entities:
            return cache

        cross_dims = [d for d in kpi_config.valid_dimensions if d.dimension_name != dim_name]
        dep_measures = [
            (dep.dependency_kpi_name, dep.pbi_measure_name)
            for dep in (kpi_config.dependencies or [])
            if dep.pbi_measure_name
        ]

        if not cross_dims and not dep_measures:
            return cache

        # ── Try consolidated UNION approach (2 DAX queries) ──────────────
        try:
            cache = await self._build_why_cache_consolidated(
                kpi_config, signal_dim_ref, dim_name, feat_name,
                period_start, period_end, job, unique_entities,
                cross_dims, dep_measures, feat_gen,
            )
            logger.info(
                "[why] consolidated cache | dims=%d deps=%d entities=%d | 2 DAX queries",
                len(cross_dims), len(dep_measures), len(unique_entities),
            )
            if not _why_cache_has_any_route_frames(cache) and (cross_dims or dep_measures):
                logger.warning(
                    "[why] consolidated cache produced no Route A/B DataFrames "
                    "(empty DAX result or column names not recognized) — "
                    "falling back to per-dimension queries",
                )
                return await self._build_why_cache_per_dimension(
                    kpi_config, signal_dim_ref, dim_name, feat_name,
                    period_start, period_end, job, unique_entities, feat_gen,
                )
            return cache
        except Exception:
            logger.warning(
                "[why] consolidated UNION failed — falling back to per-dimension queries",
                exc_info=True,
            )

        # ── Fallback: original per-dimension approach ────────────────────
        return await self._build_why_cache_per_dimension(
            kpi_config, signal_dim_ref, dim_name, feat_name,
            period_start, period_end, job, unique_entities, feat_gen,
        )

    async def _build_why_cache_consolidated(
        self,
        kpi_config,
        signal_dim_ref: DimensionRef,
        dim_name: str,
        feat_name: str,
        period_start: date,
        period_end: date,
        job: SignalJobConfig,
        unique_entities: list[str],
        cross_dims: list[DimensionRef],
        dep_measures: list[tuple[str, str]],
        feat_gen: FeatureGenerator,
    ) -> WhyDataCache:
        """Build WHY cache using 2 consolidated UNION DAX queries instead of N×2."""
        extra_filters = self._resolve_extra_filters(kpi_config, job)
        prior_start, prior_end = _prior_period_dates(feat_name, period_start, period_end)

        cur_query = build_union_why_query(
            measure=kpi_config.pbi_measure_name,
            signal_dim_ref=signal_dim_ref,
            unique_entities=unique_entities,
            cross_dims=cross_dims,
            dep_measures=dep_measures,
            date_table=feat_gen._settings.DATE_TABLE_NAME,
            date_column=feat_gen._settings.DATE_COLUMN_NAME,
            start_date=period_start,
            end_date=period_end,
            extra_filters=extra_filters,
        )
        prior_query = build_union_why_query(
            measure=kpi_config.pbi_measure_name,
            signal_dim_ref=signal_dim_ref,
            unique_entities=unique_entities,
            cross_dims=cross_dims,
            dep_measures=dep_measures,
            date_table=feat_gen._settings.DATE_TABLE_NAME,
            date_column=feat_gen._settings.DATE_COLUMN_NAME,
            start_date=prior_start,
            end_date=prior_end,
            extra_filters=extra_filters,
        )

        if not cur_query or not prior_query:
            return WhyDataCache({}, {}, {}, {})

        # Execute both consolidated queries concurrently
        cur_rows, prior_rows = await asyncio.gather(
            feat_gen._pbi.execute_dax(cur_query),
            feat_gen._pbi.execute_dax(prior_query),
        )

        cur_df = pd.DataFrame(cur_rows) if cur_rows else pd.DataFrame()
        prior_df = pd.DataFrame(prior_rows) if prior_rows else pd.DataFrame()
        cur_df = _normalize_union_why_result_df(cur_df)
        prior_df = _normalize_union_why_result_df(prior_df)

        # Defragment UNION results into per-dimension DataFrames
        route_a_cur, route_b_cur = self._defragment_union_result(cur_df, dim_name)
        route_a_pri, route_b_pri = self._defragment_union_result(prior_df, dim_name)

        return WhyDataCache(
            route_a_current=route_a_cur,
            route_a_prior={(k, feat_name): v for k, v in route_a_pri.items()},
            route_b_current=route_b_cur,
            route_b_prior={(k, feat_name): v for k, v in route_b_pri.items()},
        )

    async def _build_why_cache_per_dimension(
        self,
        kpi_config,
        signal_dim_ref: DimensionRef,
        dim_name: str,
        feat_name: str,
        period_start: date,
        period_end: date,
        job: SignalJobConfig,
        unique_entities: list[str],
        feat_gen: FeatureGenerator,
    ) -> WhyDataCache:
        """Original per-dimension WHY cache builder (fallback when UNION fails)."""
        cache = WhyDataCache({}, {}, {}, {})

        async def _fetch_route_a(other_dim: DimensionRef) -> None:
            try:
                cur_df, prior_df = await asyncio.gather(
                    self._bulk_fetch_kpi_values(
                        feat_gen, kpi_config.pbi_measure_name, signal_dim_ref, unique_entities,
                        [signal_dim_ref, other_dim], period_start, period_end, kpi_config, job
                    ),
                    self._bulk_fetch_prior_kpi_values(
                        feat_gen, kpi_config.pbi_measure_name, signal_dim_ref, unique_entities,
                        [signal_dim_ref, other_dim], feat_name, period_start, period_end, kpi_config, job
                    ),
                )
                cache.route_a_current[other_dim.dimension_name] = cur_df
                cache.route_a_prior[(other_dim.dimension_name, feat_name)] = prior_df
            except Exception:
                logger.exception("Bulk Route A failed for dim '%s'", other_dim.dimension_name)

        async def _fetch_route_b(dep) -> None:
            try:
                cur_df, prior_df = await asyncio.gather(
                    self._bulk_fetch_kpi_values(
                        feat_gen, dep.pbi_measure_name, signal_dim_ref, unique_entities,
                        [signal_dim_ref], period_start, period_end, kpi_config, job
                    ),
                    self._bulk_fetch_prior_kpi_values(
                        feat_gen, dep.pbi_measure_name, signal_dim_ref, unique_entities,
                        [signal_dim_ref], feat_name, period_start, period_end, kpi_config, job
                    ),
                )
                cache.route_b_current[dep.dependency_kpi_name] = cur_df
                cache.route_b_prior[(dep.dependency_kpi_name, feat_name)] = prior_df
            except Exception:
                logger.exception("Bulk Route B failed for dep '%s'", dep.dependency_kpi_name)

        all_fetches: list = []
        for other_dim in kpi_config.valid_dimensions:
            if other_dim.dimension_name == dim_name:
                continue
            all_fetches.append(_fetch_route_a(other_dim))

        if kpi_config.dependencies:
            for dep in kpi_config.dependencies:
                if dep.pbi_measure_name:
                    all_fetches.append(_fetch_route_b(dep))

        if all_fetches:
            await asyncio.gather(*all_fetches)

        return cache

    @staticmethod
    def _defragment_union_result(
        df: pd.DataFrame,
        signal_dim_name: str,
    ) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
        """Split a consolidated UNION DataFrame into Route A and Route B dicts.

        Returns ``(route_a, route_b)`` where:

        - ``route_a[dim_name]`` has columns ``[signal_dim_name, dim_name, 'KPI Value']``
        - ``route_b[dep_kpi_name]`` has columns ``[signal_dim_name, 'KPI Value']``
        """
        route_a: dict[str, pd.DataFrame] = {}
        route_b: dict[str, pd.DataFrame] = {}

        if df.empty or "DimensionName" not in df.columns:
            return route_a, route_b

        df = df.copy()
        df["KPIValue"] = pd.to_numeric(df["KPIValue"], errors="coerce")

        for dim_tag, group_df in df.groupby("DimensionName"):
            tag = str(dim_tag)
            sub = group_df.copy()

            if tag.startswith("__dep__"):
                dep_name = tag[len("__dep__"):]
                out = sub.rename(columns={
                    "SignalDimValue": signal_dim_name,
                    "KPIValue": "KPI Value",
                })
                route_b[dep_name] = out[[signal_dim_name, "KPI Value"]].reset_index(drop=True)
            else:
                out = sub.rename(columns={
                    "SignalDimValue": signal_dim_name,
                    "DimensionValue": tag,
                    "KPIValue": "KPI Value",
                })
                route_a[tag] = out[[signal_dim_name, tag, "KPI Value"]].reset_index(drop=True)

        return route_a, route_b

    @staticmethod
    def _resolve_extra_filters(
        kpi_config,
        job: SignalJobConfig,
    ) -> list[tuple[DimensionRef, list[str]]]:
        """Extract job filter_conditions as ``(DimensionRef, values)`` pairs for UNION query."""
        result: list[tuple[DimensionRef, list[str]]] = []
        valid_names = {d.dimension_name for d in kpi_config.valid_dimensions}
        for dim_name, vals in (job.filter_conditions or {}).items():
            dref = resolve_job_filter_dimension(kpi_config, dim_name)
            if dref is not None:
                result.append((dref, vals))
            else:
                logger.warning(
                    "[why] UNION extra filter dropped: %r not in valid_dimensions for KPI %r "
                    "(valid: %s). DAX will not apply this slice.",
                    dim_name,
                    kpi_config.kpi_name,
                    sorted(valid_names),
                )
        return result

    async def _bulk_fetch_kpi_values(
        self,
        feat_gen: FeatureGenerator,
        measure: str,
        signal_dim_ref: DimensionRef,
        unique_entities: list[str],
        group_by_dims: list[DimensionRef],
        period_start: date,
        period_end: date,
        kpi_config,
        job: SignalJobConfig,
    ) -> pd.DataFrame:
        builder = (
            DAXQueryBuilder()
            .with_kpi(measure)
            .group_by(*group_by_dims)
            .add_date_filter(
                feat_gen._settings.DATE_TABLE_NAME,
                feat_gen._settings.DATE_COLUMN_NAME,
                period_start, period_end,
            )
            .add_member_filter(signal_dim_ref, unique_entities)
        )
        builder = _apply_filters(builder, kpi_config, job.filter_conditions or {})
        query = builder.build()
        rows = await feat_gen._pbi.execute_dax(query)
        if not rows:
            return pd.DataFrame()
            
        df = pd.DataFrame(rows)
        return self._normalize_columns(df, group_by_dims)

    async def _bulk_fetch_prior_kpi_values(
        self,
        feat_gen: FeatureGenerator,
        measure: str,
        signal_dim_ref: DimensionRef,
        unique_entities: list[str],
        group_by_dims: list[DimensionRef],
        feat_name: str,
        period_start: date,
        period_end: date,
        kpi_config,
        job: SignalJobConfig,
    ) -> pd.DataFrame:
        prior_start, prior_end = _prior_period_dates(feat_name, period_start, period_end)
        builder = (
            DAXQueryBuilder()
            .with_kpi(measure)
            .group_by(*group_by_dims)
            .add_date_filter(
                feat_gen._settings.DATE_TABLE_NAME,
                feat_gen._settings.DATE_COLUMN_NAME,
                prior_start, prior_end,
            )
            .add_member_filter(signal_dim_ref, unique_entities)
        )
        builder = _apply_filters(builder, kpi_config, job.filter_conditions or {})
        query = builder.build()
        rows = await feat_gen._pbi.execute_dax(query)
        if not rows:
            return pd.DataFrame()
            
        df = pd.DataFrame(rows)
        return self._normalize_columns(df, group_by_dims)

    def _normalize_columns(self, df: pd.DataFrame, dims: list[DimensionRef]) -> pd.DataFrame:
        rename_map = {}
        for col in df.columns:
            lower = col.lower()
            for dim in dims:
                if dim.pbi_column_name.lower() in lower:
                    rename_map[col] = dim.dimension_name
            if "kpi value" in lower:
                rename_map[col] = "KPI Value"
                
        df = df.rename(columns=rename_map)
        if "KPI Value" in df.columns:
            df["KPI Value"] = pd.to_numeric(df["KPI Value"], errors="coerce")
        return df

    def _slice_entity(self, df: pd.DataFrame, signal_dim_name: str, entity_value: str, target_dim_name: str) -> dict[str, float]:
        if df.empty or signal_dim_name not in df.columns or target_dim_name not in df.columns:
            return {}
            
        df[signal_dim_name] = df[signal_dim_name].astype(str)
        filtered = df[df[signal_dim_name] == str(entity_value)]
        result = {}
        for _, row in filtered.iterrows():
            dv = str(row[target_dim_name])
            if "KPI Value" in df.columns:
                kv = row["KPI Value"]
                if kv is not None and not pd.isna(kv):
                    result[dv] = float(kv)
        return result

    # ── Routes ────────────────────────────────────────────────────────────────

    async def _route_a_cross_dimension(
        self, signal, kpi_config, cache: WhyDataCache, feat_name, sig_name, 
        threshold, threshold2, operator, kpi_format, period_start, period_end, 
        period_str, now
    ) -> list[WhyRow]:
        rows: list[WhyRow] = []
        signal_dim_name = signal.dimension
        entity_val = str(signal.dimension_value)

        for dim in kpi_config.valid_dimensions:
            if dim.dimension_name == signal_dim_name:
                continue

            try:
                cur_df = cache.route_a_current.get(dim.dimension_name, pd.DataFrame())
                prior_df = cache.route_a_prior.get((dim.dimension_name, feat_name), pd.DataFrame())

                cur_dict = self._slice_entity(cur_df, signal_dim_name, entity_val, dim.dimension_name)
                prev_dict = self._slice_entity(prior_df, signal_dim_name, entity_val, dim.dimension_name)

                if not cur_dict:
                    continue

                feature_df = _compute_feature_from_periods(
                    feat_name,
                    cur_dict,
                    prev_dict,
                    kpi_format=kpi_format,
                    kpi_name=kpi_config.kpi_name,
                )
                breaching = self._filter_breaching(
                    feature_df, feat_name, threshold, threshold2, operator,
                )
                for _, row in breaching.iterrows():
                    dim_val = str(row["dimension_value"])
                    change = row.get(feat_name)
                    change_val = float(change) if change is not None else None
                    cur_v = float(row["KPI Value"]) if row.get("KPI Value") is not None else None
                    prev_v = float(row["Prior Value"]) if row.get("Prior Value") is not None else None

                    direction = "changed"
                    if change_val is not None:
                        direction = "increased" if change_val > 0 else "decreased"
                    mag = _why_change_magnitude_phrase(kpi_config.kpi_name, change_val)
                    rationale = (
                        f"Cross-dimension {dim.dimension_name} "
                        f"{dim_val} {direction} by "
                        f"{mag}" if change_val is not None else
                        f"Cross-dimension {dim.dimension_name} {dim_val}"
                    )
                    rows.append(WhyRow(
                        why_id=str(uuid4()),
                        signal_id=signal.signal_id,
                        run_timestamp=now,
                        kpi_name=signal.kpi_name,
                        dimension_name=dim.dimension_name,
                        dimension_value=dim_val,
                        signal_name=sig_name,
                        dep_kpi_name=None,
                        dep_kpi_label=None,
                        rationale=rationale,
                        current_value=cur_v,
                        prev_value=prev_v,
                        change_pct=change_val,
                        period=period_str,
                        period_start=period_start,
                        period_end=period_end,
                    ))
            except Exception:
                logger.exception(
                    "Route A failed for dimension '%s' on signal %s",
                    dim.dimension_name, signal.signal_id,
                )
        return rows

    async def _route_b_dependencies(
        self, signal, kpi_config, cache: WhyDataCache, feat_name, sig_name, 
        threshold, threshold2, operator, kpi_format, period_start, period_end, 
        period_str, now
    ) -> list[WhyRow]:
        rows: list[WhyRow] = []
        if not kpi_config.dependencies:
            return rows

        signal_dim_name = signal.dimension
        entity_val = str(signal.dimension_value)

        for dep in kpi_config.dependencies:
            if not dep.pbi_measure_name:
                continue
            try:
                cur_df = cache.route_b_current.get(dep.dependency_kpi_name, pd.DataFrame())
                prior_df = cache.route_b_prior.get((dep.dependency_kpi_name, feat_name), pd.DataFrame())

                cur_dict = self._slice_entity(cur_df, signal_dim_name, entity_val, signal_dim_name)
                prev_dict = self._slice_entity(prior_df, signal_dim_name, entity_val, signal_dim_name)

                if not cur_dict:
                    continue

                feature_df = _compute_feature_from_periods(
                    feat_name,
                    cur_dict,
                    prev_dict,
                    kpi_format=kpi_format,
                    kpi_name=dep.dependency_kpi_name,
                )
                breaching = self._filter_breaching(
                    feature_df, feat_name, threshold, threshold2, operator,
                )
                dep_label = f"{kpi_config.kpi_name} by {signal.dimension}"
                for _, row in breaching.iterrows():
                    dim_val = str(row["dimension_value"])
                    change = row.get(feat_name)
                    change_val = float(change) if change is not None else None
                    cur_v = float(row["KPI Value"]) if row.get("KPI Value") is not None else None
                    prev_v = float(row["Prior Value"]) if row.get("Prior Value") is not None else None

                    direction = "changed"
                    if change_val is not None:
                        direction = "increased" if change_val > 0 else "decreased"
                    mag = _why_change_magnitude_phrase(dep.dependency_kpi_name, change_val)
                    rationale = (
                        f"Dependent KPI {dep.dependency_kpi_name} "
                        f"{dim_val} {direction} by "
                        f"{mag}" if change_val is not None else
                        f"Dependent KPI {dep.dependency_kpi_name} {dim_val}"
                    )
                    rows.append(WhyRow(
                        why_id=str(uuid4()),
                        signal_id=signal.signal_id,
                        run_timestamp=now,
                        kpi_name=signal.kpi_name,
                        dimension_name=signal.dimension,
                        dimension_value=dim_val,
                        signal_name=sig_name,
                        dep_kpi_name=dep.dependency_kpi_name,
                        dep_kpi_label=dep_label,
                        rationale=rationale,
                        current_value=cur_v,
                        prev_value=prev_v,
                        change_pct=change_val,
                        period=period_str,
                        period_start=period_start,
                        period_end=period_end,
                    ))
            except Exception:
                logger.exception(
                    "Route B failed for dep KPI '%s' on signal %s",
                    dep.dependency_kpi_name, signal.signal_id,
                )
        return rows

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _filter_breaching(df, feat_name: str, threshold: float,
                          threshold2: float | None, operator: str):
        """Keep only rows where the feature value breaches the threshold."""
        import pandas as pd
        feat_col = feat_name
        if feat_col not in df.columns:
            for c in df.columns:
                if feat_name in c.lower() or c.replace("calculate_", "") == feat_name:
                    feat_col = c
                    break
        if feat_col not in df.columns:
            return df.iloc[0:0]

        op_fn = _OPS.get(operator)
        if op_fn is None:
            return df.iloc[0:0]

        mask = df[feat_col].apply(
            lambda v: op_fn(v, threshold, threshold2) if v is not None and not pd.isna(v) else False
        )
        return df[mask].reset_index(drop=True)

    @staticmethod
    def _find_dim_ref(kpi_config, dim_name: str) -> DimensionRef | None:
        for d in kpi_config.valid_dimensions:
            if d.dimension_name == dim_name:
                return d
        return None
