"""Async configuration loader for the DAX KPI engine.

Reads Portal config tables from PostgreSQL via SQLAlchemy async sessions
and returns Pydantic domain models.  Results are cached with a 15-minute TTL.

Portal fork: KPI definitions are loaded from ``configkpisclientportal`` only (no derived-KPI table).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from cachetools import TTLCache
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..models.config_kpi import ConfigKPI
from ..models.config_dimension import ConfigDimension
from ..models.config_kpi_valid_dimension import ConfigKPIValidDimension
from ..models.config_dependency import ConfigKPIDependency
from ..models.config_signal import ConfigSignal
from ..models.config_signal_threshold import ConfigSignalThreshold
from ..models.config_signal_job import ConfigSignalJob
from ..models.config_feature import ConfigFeature
from ..models.portal_config_tables import (
    CONFIG_DIMENSIONS_TABLE,
    CONFIG_KPIS_TABLE,
)
from .signal_name_aliases import normalize_signal_names
from .models import (
    ConfigIncompleteError,
    DependencyConfig,
    DimensionRef,
    FeatureConfig,
    KPIConfig,
    SignalDefinition,
    SignalJobConfig,
    ThresholdConfig,
)

logger = logging.getLogger(__name__)


class ConfigLoader:
    """PostgreSQL-backed, TTL-cached config loader for the DAX engine."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory
        self._cache: TTLCache = TTLCache(maxsize=256, ttl=900)

    async def get_kpi_config(self, kpi_name: str) -> KPIConfig:
        cache_key = f"kpi_config:{kpi_name}"
        if cache_key in self._cache:
            logger.debug("Cache HIT for kpi_config:%s", kpi_name)
            return self._cache[cache_key]

        logger.debug("Loading KPI config for '%s' from DB...", kpi_name)

        async with self._sf() as session:
            kpi = await session.get(ConfigKPI, kpi_name)
            source_table = CONFIG_KPIS_TABLE

            if kpi is None:
                raise ConfigIncompleteError(
                    f"KPI '{kpi_name}' not found in {CONFIG_KPIS_TABLE}."
                )
            logger.debug(
                "  Found KPI in %s: measure=%s, pbi_measure=%s",
                source_table,
                kpi.measure,
                kpi.pbi_measure_name,
            )

            if not kpi.pbi_measure_name:
                raise ConfigIncompleteError(
                    f"KPI '{kpi_name}' has no pbi_measure_name. "
                    f"Populate {source_table}.pbi_measure_name "
                    f"to use the DAX engine."
                )

            stmt = (
                select(ConfigKPIValidDimension, ConfigDimension)
                .join(
                    ConfigDimension,
                    ConfigKPIValidDimension.dimension_name == ConfigDimension.dimension_name,
                )
                .where(ConfigKPIValidDimension.kpi_name == kpi_name)
                .where(ConfigKPIValidDimension.is_valid.is_(True))
            )
            result = await session.execute(stmt)
            dim_rows = result.all()
            logger.debug("  Found %d valid dimension(s) for '%s'", len(dim_rows), kpi_name)

            dimensions: list[DimensionRef] = []
            for vd, dim in dim_rows:
                if not dim.pbi_table_name or not dim.pbi_column_name:
                    raise ConfigIncompleteError(
                        f"Dimension '{dim.dimension_name}' missing pbi_table_name or "
                        f"pbi_column_name in {CONFIG_DIMENSIONS_TABLE}. Run:\n"
                        f"  UPDATE {CONFIG_DIMENSIONS_TABLE}\n"
                        f"  SET pbi_table_name = '<table>', pbi_column_name = '<col>'\n"
                        f"  WHERE dimension_name = '{dim.dimension_name}';"
                    )
                ref = DimensionRef(
                    dimension_name=dim.dimension_name,
                    pbi_table_name=dim.pbi_table_name,
                    pbi_column_name=dim.pbi_column_name,
                )
                logger.debug(
                    "    dim: %s → %s[%s]",
                    dim.dimension_name,
                    dim.pbi_table_name,
                    dim.pbi_column_name,
                )
                dimensions.append(ref)

            stmt_deps = select(ConfigKPIDependency).where(
                ConfigKPIDependency.parent_kpi == kpi_name
            )
            dep_result = await session.execute(stmt_deps)
            dep_rows = dep_result.scalars().all()

            dependencies = [
                DependencyConfig(
                    dependency_kpi_name=d.dependent_kpi,
                    pbi_measure_name=getattr(d, "pbi_measure_name", None),
                )
                for d in dep_rows
            ]

        config = KPIConfig(
            kpi_name=kpi_name,
            pbi_measure_name=kpi.pbi_measure_name,
            kpi_format=getattr(kpi, "format", "number") or "number",
            valid_dimensions=dimensions,
            dependencies=dependencies,
        )
        self._cache[cache_key] = config
        return config

    async def get_signal_thresholds(
        self, kpi_name: str, dimension_name: str
    ) -> list[ThresholdConfig]:
        cache_key = f"thresholds:{kpi_name}:{dimension_name}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        async with self._sf() as session:
            # 1) Prefer dedicated threshold rows (exact dimension match)
            stmt_exact = (
                select(ConfigSignalThreshold)
                .where(ConfigSignalThreshold.kpi_name == kpi_name)
                .where(ConfigSignalThreshold.dimension_name == dimension_name)
            )
            exact_result = await session.execute(stmt_exact)
            exact_rows = list(exact_result.scalars().all())

            if exact_rows:
                thresholds = [
                    ThresholdConfig(
                        kpi_name=kpi_name,
                        dimension_name=dimension_name,
                        operator=row.operator,
                        threshold_value=float(row.threshold),
                        severity=row.severity,
                    )
                    for row in exact_rows
                ]
                self._cache[cache_key] = thresholds
                return thresholds

            # 2) KPI-level wildcard rows (dimension_name IS NULL)
            stmt_wild = (
                select(ConfigSignalThreshold)
                .where(ConfigSignalThreshold.kpi_name == kpi_name)
                .where(ConfigSignalThreshold.dimension_name.is_(None))
            )
            wild_result = await session.execute(stmt_wild)
            wild_rows = list(wild_result.scalars().all())

            if wild_rows:
                thresholds = [
                    ThresholdConfig(
                        kpi_name=kpi_name,
                        dimension_name=dimension_name,
                        operator=row.operator,
                        threshold_value=float(row.threshold),
                        severity=row.severity,
                    )
                    for row in wild_rows
                ]
                self._cache[cache_key] = thresholds
                return thresholds

            # 3) Fallback: active jobs → signal names → config_signals
            stmt = select(ConfigSignalJob).where(
                ConfigSignalJob.kpi_name == kpi_name,
                ConfigSignalJob.is_active.is_(True),
            )
            result = await session.execute(stmt)
            jobs = result.scalars().all()

            signal_names: set[str] = set()
            for job in jobs:
                if isinstance(job.signals, list):
                    signal_names.update(normalize_signal_names(job.signals))

            if not signal_names:
                self._cache[cache_key] = []
                return []

            stmt_sig = select(ConfigSignal).where(
                ConfigSignal.signal_name.in_(signal_names)
            )
            sig_result = await session.execute(stmt_sig)
            signals = sig_result.scalars().all()

        thresholds: list[ThresholdConfig] = []
        for sig in signals:
            threshold_value = float(sig.threshold)
            if sig.dimensions and isinstance(sig.dimensions, dict):
                if dimension_name in sig.dimensions:
                    threshold_value = float(sig.dimensions[dimension_name])

            thresholds.append(
                ThresholdConfig(
                    kpi_name=kpi_name,
                    dimension_name=dimension_name,
                    operator=sig.operator,
                    threshold_value=threshold_value,
                    severity=sig.severity,
                )
            )

        self._cache[cache_key] = thresholds
        return thresholds

    async def get_signal_definitions(
        self, signal_names: list[str]
    ) -> list[SignalDefinition]:
        """Load signal definitions from config_signalsclientportal by name."""
        signal_names = normalize_signal_names(signal_names)
        if not signal_names:
            return []
        cache_key = f"sig_defs:{','.join(sorted(signal_names))}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        async with self._sf() as session:
            stmt = select(ConfigSignal).where(
                ConfigSignal.signal_name.in_(signal_names)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        defs = [
            SignalDefinition(
                signal_name=r.signal_name,
                feature_name=r.feature_name,
                operator=r.operator,
                threshold=float(r.threshold),
                threshold2=float(r.threshold2) if r.threshold2 is not None else None,
                severity=r.severity,
                message_template=r.message_template or "",
                format=r.format or "percentage",
            )
            for r in rows
        ]
        logger.debug("Loaded %d signal definition(s) for %s", len(defs), signal_names)
        self._cache[cache_key] = defs
        return defs

    async def get_signal_jobs(
        self, active_only: bool = True
    ) -> list[SignalJobConfig]:
        cache_key = f"signal_jobs:{active_only}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        async with self._sf() as session:
            stmt = select(ConfigSignalJob)
            if active_only:
                stmt = stmt.where(ConfigSignalJob.is_active.is_(True))
            result = await session.execute(stmt)
            rows = result.scalars().all()

        configs: list[SignalJobConfig] = []
        for row in rows:
            filter_conditions, period_raw = _parse_filters_and_period(row.filters)
            logger.debug(
                "Job %s raw_filters=%s → parsed=%s, period=%s",
                row.job_id, row.filters, filter_conditions, period_raw
            )

            p_start = None
            p_end = None
            if period_raw.get("start"):
                try:
                    p_start = datetime.strptime(period_raw["start"], "%Y-%m-%d").date()
                except ValueError:
                    logger.warning("Job %s: invalid period start date '%s'", row.job_id, period_raw["start"])
            if period_raw.get("end"):
                try:
                    p_end = datetime.strptime(period_raw["end"], "%Y-%m-%d").date()
                except ValueError:
                    logger.warning("Job %s: invalid period end date '%s'", row.job_id, period_raw["end"])


            dims = row.dimensions if isinstance(row.dimensions, list) else []
            feats = row.features if isinstance(row.features, list) else []
            sigs = normalize_signal_names(
                row.signals if isinstance(row.signals, list) else []
            )
            for dim_name in (dims or [""]):
                configs.append(
                    SignalJobConfig(
                        job_id=str(row.job_id),
                        kpi_name=row.kpi_name,
                        dimension_name=dim_name,
                        features=feats,
                        signals=sigs,
                        frequency_minutes=60,
                        filter_conditions=filter_conditions,
                        is_active=row.is_active,
                        period_start=p_start,
                        period_end=p_end,
                    )
                )

        self._cache[cache_key] = configs
        return configs

    async def get_feature_configs(
        self, kpi_name: str
    ) -> list[FeatureConfig]:
        cache_key = f"feature_configs:{kpi_name}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        async with self._sf() as session:
            stmt = select(ConfigSignalJob).where(
                ConfigSignalJob.kpi_name == kpi_name,
                ConfigSignalJob.is_active.is_(True),
            )
            job_result = await session.execute(stmt)
            jobs = job_result.scalars().all()

            feature_names: set[str] = set()
            job_dims: list[str] = []
            for job in jobs:
                if isinstance(job.features, list):
                    feature_names.update(job.features)
                if isinstance(job.dimensions, list):
                    job_dims.extend(job.dimensions)

            if not feature_names:
                self._cache[cache_key] = []
                return []

            stmt_feat = select(ConfigFeature).where(
                ConfigFeature.feature_name.in_(feature_names)
            )
            feat_result = await session.execute(stmt_feat)
            features = feat_result.scalars().all()

        first_dim = job_dims[0] if job_dims else ""
        configs = [
            FeatureConfig(
                feature_name=f.feature_name,
                kpi_name=kpi_name,
                dimension_name=first_dim,
                column_alias_map=(
                    f.column_alias_map
                    if hasattr(f, "column_alias_map") and f.column_alias_map
                    else {}
                ),
            )
            for f in features
        ]

        self._cache[cache_key] = configs
        return configs

    async def resolve_dimension_ref(self, dimension_name: str) -> DimensionRef | None:
        """Look up a single DimensionRef by name."""
        cache_key = f"dim_ref:{dimension_name}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        async with self._sf() as session:
            stmt = select(ConfigDimension).where(
                ConfigDimension.dimension_name == dimension_name
            )
            result = await session.execute(stmt)
            dim = result.scalar_one_or_none()
            if dim is None or not dim.pbi_table_name or not dim.pbi_column_name:
                return None
            ref = DimensionRef(
                dimension_name=dim.dimension_name,
                pbi_table_name=dim.pbi_table_name,
                pbi_column_name=dim.pbi_column_name,
            )
            self._cache[cache_key] = ref
            return ref



def _parse_filters_and_period(raw_filters: Any) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Convert job JSONB filters to ``({dim: [values]}, {"start": "...", "end": "..."})``.

    Handles job filter JSON, e.g.:
      {"period": {"start": "...", "end": "..."}, "Region": "West", ...}
    The "period" key is extracted into the second dictionary.
    Scalar values are wrapped in a list; lists are kept as-is.
    """
    if not raw_filters or not isinstance(raw_filters, dict):
        return {}, {}
    filters: dict[str, list[str]] = {}
    period: dict[str, str] = {}
    for key, val in raw_filters.items():
        if key == "period":
            if isinstance(val, dict):
                period = {str(k): str(v) for k, v in val.items()}
            continue
        if isinstance(val, list):
            filters[key] = [str(v) for v in val]
        elif isinstance(val, dict):
            continue
        else:
            filters[key] = [str(val)]
    return filters, period

