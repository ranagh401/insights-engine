"""KPI engine — Phase 1 (per-dimension fetch) and Phase 2 (WHY routes A & B).

Orchestrates DAX query building, concurrent execution via PBIClient,
response parsing, and result persistence.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from ..config.config_loader import ConfigLoader
from ..config.models import (
    DimensionRef,
    DrillDimensionResult,
    KPIConfig,
    KPIRow,
    Signal,
    SignalJobConfig,
    resolve_job_filter_dimension,
)
from ..dax.query_builder import DAXQueryBuilder
from ..powerbi.api_client import PBIClient
from ..settings import Settings
from ..store.result_store import ResultStore

logger = logging.getLogger(__name__)


class KPIEngine:
    """Executes DAX queries for KPI values and WHY analysis."""

    def __init__(
        self,
        config_loader: ConfigLoader,
        pbi_client: PBIClient,
        result_store: ResultStore,
        settings: Settings,
    ) -> None:
        self._loader = config_loader
        self._pbi = pbi_client
        self._store = result_store
        self._settings = settings

    async def run_phase1(
        self,
        job: SignalJobConfig,
        start_date: date,
        end_date: date,
    ) -> dict[str, list[KPIRow]]:
        """Fetch KPI values for ALL valid dimensions concurrently.

        Returns ``{dimension_name: [KPIRow, ...]}``.
        """
        try:
            config = await self._loader.get_kpi_config(job.kpi_name)
        except Exception:
            logger.error(
                "Job %s FAILED — cannot load KPI config for '%s'.",
                job.job_id,
                job.kpi_name,
                exc_info=True,
            )
            return {}
        logger.info(
            "Job %s loaded config for '%s': measure=%s, dims=%s, deps=%s",
            job.job_id,
            job.kpi_name,
            config.pbi_measure_name,
            [d.dimension_name for d in config.valid_dimensions],
            [d.dependency_kpi_name for d in config.dependencies],
        )

        queries: dict[str, str] = {}
        dim_map: dict[str, DimensionRef] = {}

        for dim in config.valid_dimensions:
            builder = (
                DAXQueryBuilder()
                .with_kpi(config.pbi_measure_name)
                .group_by(dim)
                .add_date_filter(
                    self._settings.DATE_TABLE_NAME,
                    self._settings.DATE_COLUMN_NAME,
                    start_date,
                    end_date,
                )
            )
            for fc_dim_name, fc_values in job.filter_conditions.items():
                fc_ref = _resolve_filter_dim(config, fc_dim_name)
                if fc_ref:
                    builder = builder.add_member_filter(fc_ref, fc_values)

            queries[dim.dimension_name] = builder.build()
            dim_map[dim.dimension_name] = dim

        if not queries:
            return {}

        dim_names = list(queries.keys())
        dax_queries = [queries[d] for d in dim_names]
        responses = await self._pbi.execute_dax_batch(dax_queries)

        output: dict[str, list[KPIRow]] = {}
        for dim_name, raw_rows in zip(dim_names, responses):
            dim_ref = dim_map[dim_name]
            kpi_rows = _parse_dax_response(
                raw_rows,
                config.kpi_name,
                dim_ref,
                grain=dim_name,
                period_start=start_date,
                period_end=end_date,
            )
            output[dim_name] = kpi_rows
            await self._store.write_kpi_results(kpi_rows, job.job_id)

        return output

    async def run_phase2_route_a(
        self,
        signal: Signal,
        job: SignalJobConfig,
        kpi_config: KPIConfig,
        period_start: date,
        period_end: date,
    ) -> list[DrillDimensionResult]:
        """Route A — dimension drill-down for the breaching entity."""
        signal_dim_ref = _find_dim_ref(kpi_config, signal.dimension)
        results: list[DrillDimensionResult] = []

        for dim in kpi_config.valid_dimensions:
            if dim.dimension_name == signal.dimension:
                continue

            cached = await self._store.get_kpi_results(
                kpi_config.kpi_name,
                dim.dimension_name,
                period_start,
                period_end,
            )

            if cached:
                filtered = [
                    r
                    for r in cached
                    if r.dimension_values.get(signal.dimension) == signal.dimension_value
                ]
                if not filtered:
                    filtered = cached
                drill_rows = filtered
            else:
                builder = (
                    DAXQueryBuilder()
                    .with_kpi(kpi_config.pbi_measure_name)
                    .group_by(dim)
                    .add_date_filter(
                        self._settings.DATE_TABLE_NAME,
                        self._settings.DATE_COLUMN_NAME,
                        period_start,
                        period_end,
                    )
                )
                if signal_dim_ref:
                    builder = builder.add_entity_pin_filter(
                        signal_dim_ref, signal.dimension_value
                    )
                for fc_dim_name, fc_values in job.filter_conditions.items():
                    fc_ref = _resolve_filter_dim(kpi_config, fc_dim_name)
                    if fc_ref:
                        builder = builder.add_member_filter(fc_ref, fc_values)

                raw = await self._pbi.execute_dax(builder.build())
                drill_rows = _parse_dax_response(
                    raw,
                    kpi_config.kpi_name,
                    dim,
                    grain=dim.dimension_name,
                    period_start=period_start,
                    period_end=period_end,
                )
                await self._store.write_kpi_results(drill_rows, job.job_id)

            sorted_rows = sorted(
                drill_rows,
                key=lambda r: r.kpi_value if r.kpi_value is not None else float("inf"),
            )

            values = [r.kpi_value for r in sorted_rows if r.kpi_value is not None]
            avg_val = sum(values) / len(values) if values else 0.0
            below_avg = [
                r
                for r in sorted_rows
                if r.kpi_value is not None and r.kpi_value < avg_val
            ]
            top_contributors = [
                next(iter(r.dimension_values.values()), "")
                for r in below_avg[:3]
            ]

            results.append(
                DrillDimensionResult(
                    dimension=dim.dimension_name,
                    result_type="drill_down",
                    rows=sorted_rows,
                    top_contributors=top_contributors,
                )
            )

        return results

    async def run_phase2_route_b(
        self,
        signal: Signal,
        job: SignalJobConfig,
        kpi_config: KPIConfig,
        period_start: date,
        period_end: date,
    ) -> list[DrillDimensionResult]:
        """Route B — KPI dependency component check."""
        if not kpi_config.dependencies:
            return []

        signal_dim_ref = _find_dim_ref(kpi_config, signal.dimension)
        queries: list[str] = []
        dep_names: list[str] = []

        for dep in kpi_config.dependencies:
            if not dep.pbi_measure_name:
                logger.warning(
                    "Dependency KPI '%s' has no pbi_measure_name in "
                    "configkpidependenciesclientportal. Skipping Route B "
                    "for this dependency.",
                    dep.dependency_kpi_name,
                )
                continue

            builder = (
                DAXQueryBuilder()
                .with_kpi(dep.pbi_measure_name, alias=dep.dependency_kpi_name)
                .add_date_filter(
                    self._settings.DATE_TABLE_NAME,
                    self._settings.DATE_COLUMN_NAME,
                    period_start,
                    period_end,
                )
            )
            if signal_dim_ref:
                builder = builder.group_by(signal_dim_ref)
                builder = builder.add_entity_pin_filter(
                    signal_dim_ref, signal.dimension_value
                )
            for fc_dim_name, fc_values in job.filter_conditions.items():
                fc_ref = _resolve_filter_dim(kpi_config, fc_dim_name)
                if fc_ref:
                    builder = builder.add_member_filter(fc_ref, fc_values)

            queries.append(builder.build())
            dep_names.append(dep.dependency_kpi_name)

        if not queries:
            return []

        responses = await self._pbi.execute_dax_batch(queries)
        results: list[DrillDimensionResult] = []

        for dep_name, raw_rows in zip(dep_names, responses):
            kpi_rows: list[KPIRow] = []
            for row in raw_rows:
                dim_val = _extract_dim_value(row, signal_dim_ref) if signal_dim_ref else ""
                kpi_val = _extract_measure_value(row, dep_name)
                kpi_rows.append(
                    KPIRow(
                        dimension_values={signal.dimension: dim_val},
                        kpi_name=kpi_config.kpi_name,
                        kpi_value=kpi_val,
                        grain=signal.dimension,
                        period_start=period_start,
                        period_end=period_end,
                        computed_at=datetime.now(timezone.utc),
                        result_type="dependency",
                        dependency_kpi_name=dep_name,
                    )
                )
            await self._store.write_kpi_results(kpi_rows, job.job_id)

            results.append(
                DrillDimensionResult(
                    dimension=signal.dimension,
                    result_type="dependency",
                    rows=kpi_rows,
                    top_contributors=[],
                )
            )

        return results


def _resolve_filter_dim(config: KPIConfig, dim_name: str) -> DimensionRef | None:
    return resolve_job_filter_dimension(config, dim_name)


def _find_dim_ref(config: KPIConfig, dim_name: str) -> DimensionRef | None:
    return _resolve_filter_dim(config, dim_name)


def _parse_dax_response(
    raw_rows: list[dict[str, Any]],
    kpi_name: str,
    dim_ref: DimensionRef,
    *,
    grain: str,
    period_start: date,
    period_end: date,
    measure_alias: str = "KPI Value",
    result_type: str = "kpi",
    dependency_kpi_name: str | None = None,
) -> list[KPIRow]:
    """Convert Power BI Execute Queries response rows into KPIRow list."""
    rows: list[KPIRow] = []
    for raw in raw_rows:
        dim_val = _extract_dim_value(raw, dim_ref)
        kpi_val = _extract_measure_value(raw, measure_alias)
        rows.append(
            KPIRow(
                dimension_values={dim_ref.dimension_name: dim_val},
                kpi_name=kpi_name,
                kpi_value=kpi_val,
                grain=grain,
                period_start=period_start,
                period_end=period_end,
                computed_at=datetime.now(timezone.utc),
                result_type=result_type,
                dependency_kpi_name=dependency_kpi_name,
            )
        )
    return rows


def _extract_dim_value(row: dict[str, Any], dim_ref: DimensionRef) -> str:
    """Find the dimension value in a DAX response row.

    PBI Execute Queries returns keys without single quotes around the table name
    and may differ in column capitalisation from the model definition, e.g.:
      '[DS0] Xref Market (Unified)[Market for OpCo Reporting]'
    Try exact candidates first, then fall back to a case-insensitive scan.
    """
    candidates = [
        dim_ref.pbi_expression,                                           # 'Table'[Column]
        f"{dim_ref.pbi_table_name}[{dim_ref.pbi_column_name}]",          # Table[Column]
        dim_ref.pbi_column_name,                                          # Column only
    ]
    for key in candidates:
        if key in row:
            return str(row[key]) if row[key] is not None else ""

    col_lower = dim_ref.pbi_column_name.lower()
    for key in row:
        if col_lower in key.lower():
            return str(row[key]) if row[key] is not None else ""
    return ""


def _extract_measure_value(row: dict[str, Any], alias: str) -> float | None:
    """Find the measure value by alias in a DAX response row."""
    candidates = [f"[{alias}]", alias]
    for key in candidates:
        if key in row:
            val = row[key]
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None
    for key in row:
        if alias in key:
            val = row[key]
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None
    return None
