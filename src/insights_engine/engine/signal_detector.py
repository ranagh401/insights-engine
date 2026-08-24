"""Signal detector — computes features and evaluates against signal definitions.

Flow per job:
  1. Load signal definitions from config_signalsclientportal for the job's signal list
  2. Group signal definitions by feature_name
  3. Compute each required feature via FeatureGenerator (one DAX call per feature)
  4. Compare feature values against thresholds
  5. Persist breaching signals to signal_log
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timezone
from uuid import uuid4

import pandas as pd

from ..config.config_loader import ConfigLoader
from ..config.models import Signal, SignalDefinition, SignalJobConfig
from ..engine.feature_generator import FeatureGenerator
from ..store.result_store import ResultStore

logger = logging.getLogger(__name__)

_OPS = {
    "lt": lambda v, t: v < t,
    "gt": lambda v, t: v > t,
    "lte": lambda v, t: v <= t,
    "gte": lambda v, t: v >= t,
    "between": lambda v, t1, t2: t1 <= v <= t2,
}


class SignalDetector:
    """Feature-aware signal evaluation engine."""

    def __init__(
        self,
        config_loader: ConfigLoader,
        feature_generator: FeatureGenerator,
        result_store: ResultStore,
    ) -> None:
        self._loader = config_loader
        self._features = feature_generator
        self._store = result_store

    async def detect_signals(
        self,
        job: SignalJobConfig,
        start_date,
        end_date,
    ) -> list[Signal]:
        """Run the full feature→threshold→signal pipeline for one job.

        Returns list of newly detected Signal objects.
        """
        if not job.signals:
            logger.warning("Job %s has no signals configured. Skipping.", job.job_id)
            return []

        sig_defs = await self._loader.get_signal_definitions(job.signals)
        if not sig_defs:
            logger.warning(
                "Job %s: none of %s found in config_signalsclientportal. Skipping.",
                job.job_id, job.signals,
            )
            return []

        feature_to_sigs: dict[str, list[SignalDefinition]] = defaultdict(list)
        for sd in sig_defs:
            feature_to_sigs[sd.feature_name].append(sd)

        required_features = set(feature_to_sigs.keys())
        logger.debug(
            "Job %s: %d signal def(s), %d unique feature(s) needed: %s",
            job.job_id, len(sig_defs), len(required_features), sorted(required_features),
        )

        feature_dfs = await self._features.generate_features_for_kpi(
            kpi_name=job.kpi_name,
            dimension_name=job.dimension_name,
            start_date=start_date,
            end_date=end_date,
            filter_conditions=job.filter_conditions or {},
            only_feature_names=required_features,
        )

        if not feature_dfs:
            logger.info(
                "Job %s: no computable feature data for kpi=%s dim=%s (e.g. missing prior period) — skipping.",
                job.job_id, job.kpi_name, job.dimension_name,
            )
            return []

        logger.debug(
            "Job %s: FeatureGenerator returned features: %s",
            job.job_id, list(feature_dfs.keys()),
        )

        dax_kpi_query = self._features.last_kpi_dax_query
        per_feature_dax = self._features.feature_dax_queries
        current_period_kpi = self._features.current_period_kpi
        prior_period_kpi = self._features.prior_period_kpi

        all_signals: list[Signal] = []
        now = datetime.now(timezone.utc)

        for feat_name, sig_def_list in feature_to_sigs.items():
            df = _lookup_feature_df(feature_dfs, feat_name)
            if df is None or df.empty:
                logger.info(
                    "Job %s: feature '%s' not computed (no prior-period data) — skipping %d signal(s).",
                    job.job_id, feat_name, len(sig_def_list),
                )
                continue

            feat_dax_query = _resolve_feature_dax(per_feature_dax, feat_name)

            dim_col = "dimension_value"
            if dim_col not in df.columns:
                for c in df.columns:
                    if "dimension" in c.lower() or job.dimension_name.lower() in c.lower():
                        dim_col = c
                        break

            feat_col = _find_feature_column(df, feat_name)
            if feat_col is None:
                logger.warning(
                    "Job %s: column '%s' not in feature DataFrame (cols=%s). Skipping.",
                    job.job_id, feat_name, list(df.columns),
                )
                continue

            for _, row in df.iterrows():
                dim_val = str(row.get(dim_col, "")) if dim_col in df.columns else ""
                feat_val = row.get(feat_col)
                if feat_val is None or pd.isna(feat_val):
                    continue
                feat_val = float(feat_val)

                # ── current_kpi_value: always from the current-period (exact
                # date-range) fetch so it matches what dax_kpi_query returns.
                cur_kpi = current_period_kpi.get(dim_val)

                # ── prev_kpi_value: from executed prior-period DAX for simple
                # features (wow_growth_pct, kpi_vs_rolling_avg_pct, etc.).
                # The dict key may have a "calculate_" prefix depending on how
                # FEATURE_FUNCTION_REGISTRY registered the feature.
                prev_kpi: float | None = None
                for _key in (feat_name, f"calculate_{feat_name}"):
                    feat_prior_map = prior_period_kpi.get(_key)
                    if feat_prior_map is not None:
                        v = feat_prior_map.get(dim_val)
                        if v is not None:
                            prev_kpi = float(v)
                        break

                if prev_kpi is None:
                    # Fallback: try "Prior Value" column in the DataFrame row
                    # (set by _compute_feature_from_periods or batch path).
                    pv = row.get("Prior Value")
                    if pv is not None and not pd.isna(pv):
                        prev_kpi = float(pv)

                for sd in sig_def_list:
                    threshold = sd.threshold
                    if sd.operator == "between":
                        if sd.threshold2 is not None and _OPS["between"](feat_val, threshold, sd.threshold2):
                            breach = feat_val - threshold
                            all_signals.append(_build_signal(
                                job, sd, feat_name, feat_val, dim_val, threshold, breach, now,
                                dax_kpi_query, feat_dax_query,
                                current_kpi_value=cur_kpi, prev_kpi_value=prev_kpi,
                            ))
                    else:
                        op_fn = _OPS.get(sd.operator)
                        if op_fn is None:
                            logger.warning("Unknown operator '%s' for signal '%s'.", sd.operator, sd.signal_name)
                            continue
                        if op_fn(feat_val, threshold):
                            breach = round(feat_val - threshold, 6)
                            all_signals.append(_build_signal(
                                job, sd, feat_name, feat_val, dim_val, threshold, breach, now,
                                dax_kpi_query, feat_dax_query,
                                current_kpi_value=cur_kpi, prev_kpi_value=prev_kpi,
                            ))

        if all_signals:
            logger.info(
                "[signals] writing to signal_log | job_id=%s kpi=%s dim=%s rows=%d",
                job.job_id,
                job.kpi_name,
                job.dimension_name,
                len(all_signals),
            )
            await self._store.write_signals(all_signals, job.job_id)
            logger.info(
                "[signals] committed | job_id=%s kpi=%s dim=%s rows=%d",
                job.job_id,
                job.kpi_name,
                job.dimension_name,
                len(all_signals),
            )
        else:
            logger.info(
                "[signals] no breaches | job_id=%s kpi=%s dim=%s",
                job.job_id,
                job.kpi_name,
                job.dimension_name,
            )

        return all_signals


def _resolve_feature_dax(
    per_feature_dax: dict[str, str | None], feat_name: str
) -> str | None:
    """Look up the extra DAX query fired for a feature, handling name variants."""
    if feat_name in per_feature_dax:
        return per_feature_dax[feat_name]
    prefixed = f"calculate_{feat_name}"
    if prefixed in per_feature_dax:
        return per_feature_dax[prefixed]
    for key in per_feature_dax:
        if feat_name in key or key.replace("calculate_", "") == feat_name:
            return per_feature_dax[key]
    return None


def _lookup_feature_df(
    feature_dfs: dict[str, pd.DataFrame], feat_name: str
) -> pd.DataFrame | None:
    """Match config feature name (e.g. 'wow_growth_pct') to FeatureGenerator
    output keys (e.g. 'calculate_wow_growth_pct')."""
    if feat_name in feature_dfs:
        return feature_dfs[feat_name]
    prefixed = f"calculate_{feat_name}"
    if prefixed in feature_dfs:
        return feature_dfs[prefixed]
    for key in feature_dfs:
        if feat_name in key or key.replace("calculate_", "") == feat_name:
            return feature_dfs[key]
    return None


def _find_feature_column(df: pd.DataFrame, feat_name: str) -> str | None:
    """Find the column that holds the computed feature value."""
    candidates = [
        feat_name,
        f"calculate_{feat_name}",
    ]
    for c in candidates:
        if c in df.columns:
            return c
    for c in df.columns:
        if feat_name in c.lower() or c.replace("calculate_", "") == feat_name:
            return c
    return None


def _build_signal(
    job: SignalJobConfig,
    sd: SignalDefinition,
    feat_name: str,
    feat_val: float,
    dim_val: str,
    threshold: float,
    breach: float,
    now: datetime,
    dax_kpi_query: str | None = None,
    dax_feature_query: str | None = None,
    current_kpi_value: float | None = None,
    prev_kpi_value: float | None = None,
) -> Signal:
    
    # Round all numeric values to 2 decimal places for clean output.
    feat_val = round(feat_val, 2)
    breach = round(breach, 2)
    if current_kpi_value is not None:
        current_kpi_value = round(current_kpi_value, 2)
    if prev_kpi_value is not None:
        prev_kpi_value = round(prev_kpi_value, 2)

    return Signal(
        signal_id=str(uuid4()),
        kpi_name=job.kpi_name,
        dimension=job.dimension_name,
        dimension_value=dim_val,
        signal_name=sd.signal_name,
        feature_name=feat_name,
        feature_value=feat_val,
        current_kpi_value=current_kpi_value,
        prev_kpi_value=prev_kpi_value,
        observed_value=feat_val,
        threshold_value=threshold,
        operator=sd.operator,
        severity=sd.severity,
        breach_delta=breach,
        detected_at=now,
        why_computed=False,
        job_id=job.job_id,
        dax_kpi_query=dax_kpi_query,
        dax_feature_query=dax_feature_query,
    )
