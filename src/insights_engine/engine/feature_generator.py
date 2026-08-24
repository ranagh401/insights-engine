"""Feature generator — DAX-optimized with Python fallback.

Strategy:
  1. **Cross-sectional features** (rank, mix, z-score, etc.) need only
     current-period data grouped by dimension → ONE DAX call.
  2. **Time-series features** (growth, rolling_avg, acceleration, etc.)
     need historical data → ONE DAX call with extended date range and
     time-grain grouping, then Python calculators applied.
  3. On any DAX or Python failure, the feature is logged and skipped.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from typing import Any

import numpy as np
import pandas as pd

from ..config.config_loader import ConfigLoader
from ..config.dimension_constants import (
    SELF_DIMENSION_NAME,
    SELF_DIMENSION_VALUE,
    is_self_dimension,
    is_self_feature,
)
from ..config.models import DimensionRef, resolve_job_filter_dimension
from ..dax.feature_dax_definitions import (
    FEATURE_STRATEGIES,
    FeatureDAXStrategy,
    _get_strategy,
    _normalize_feature_name,
    is_simple_dax_feature,
    max_lookback_months,
    split_by_category,
)
from ..dax.query_builder import DAXQueryBuilder
from ..definitions.features import FEATURE_FUNCTION_REGISTRY
from ..powerbi.api_client import PBIClient
from ..settings import Settings
from .metric_display import (
    kpi_storage_to_percent_point_scale,
    kpi_uses_percentage_point_difference,
)

logger = logging.getLogger(__name__)


def _run_log_prefix(run_label: str) -> str:
    return f"[{run_label}] " if run_label else ""


_FEATURE_VALUE_DECIMAL_PLACES = 2


def _to_feature_float(val: Any) -> float:
    """Round a numeric KPI / feature scalar to a fixed number of decimal places."""
    return round(float(val), _FEATURE_VALUE_DECIMAL_PLACES)


def _to_feature_float_optional(val: Any) -> float | None:
    """Same as ``_to_feature_float`` but returns ``None`` for missing or non-finite values."""
    if val is None:
        return None
    if isinstance(val, float) and np.isnan(val):
        return None
    try:
        if pd.isna(val):
            return None
    except TypeError:
        pass
    try:
        return round(float(val), _FEATURE_VALUE_DECIMAL_PLACES)
    except (TypeError, ValueError):
        return None


def _round_numeric_columns_in_feature_df(
    df: pd.DataFrame, *, decimals: int = _FEATURE_VALUE_DECIMAL_PLACES
) -> pd.DataFrame:
    """Round numeric columns in feature result frames (KPI Value, Prior Value, feature columns)."""
    if df.empty:
        return df
    out = df.copy()
    for col in out.columns:
        if col == "dimension_value":
            continue
        if str(col).startswith("_"):
            continue
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            continue
        if pd.api.types.is_numeric_dtype(out[col]):
            out[col] = out[col].round(decimals)
    return out


def _registry_keys_for_requested_features(
    only: Collection[str] | None,
) -> list[str] | None:
    """Map config / DB feature names to ``FEATURE_FUNCTION_REGISTRY`` keys.

    Returns ``None`` when ``only`` is omitted (compute all registered features).
    """
    if only is None:
        return None
    norm_req = {_normalize_feature_name(str(x)) for x in only if str(x).strip()}
    if not norm_req:
        return None
    return [
        k
        for k in FEATURE_FUNCTION_REGISTRY
        if _normalize_feature_name(k) in norm_req
    ]


class FeatureGenerator:
    """Generate features by executing batched DAX queries and applying Python logic."""

    def __init__(
        self,
        config_loader: ConfigLoader,
        pbi_client: PBIClient,
        settings: Settings,
        *,
        run_label: str | None = None,
    ) -> None:
        self._loader = config_loader
        self._pbi = pbi_client
        self._settings = settings
        # e.g. "job 15/217 id=132 btd_amount_mrk/Branch_Name" — attached to DEBUG DAX logs
        self._run_label = (run_label or "").strip()
        self.last_kpi_dax_query: str | None = None
        self.feature_dax_queries: dict[str, str | None] = {}
        # dim_value → KPI Value from the current-period (weekly/daily) fetch.
        # Always matches what dax_kpi_query returns in Power BI.
        self.current_period_kpi: dict[str, float] = {}
        # feat_name → {dim_value → prior-period KPI Value}.
        # Populated for SIMPLE_DAX_FEATURES by executing dax_feature_query.
        self.prior_period_kpi: dict[str, dict[str, float]] = {}
        # KPI format from config (e.g. "percentage", "number") — affects growth calc.
        self.last_kpi_format: str = "number"

    async def generate_features_for_kpi(
        self,
        kpi_name: str,
        dimension_name: str,
        start_date: date,
        end_date: date,
        filter_conditions: dict[str, list[str]] | None = None,
        *,
        only_feature_names: Collection[str] | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Return ``{feature_name: DataFrame}`` for features of a KPI.

        When ``only_feature_names`` is set (e.g. from signal definitions), only
        those registry-backed features are computed — large wall-time savings vs
        running every entry in ``FEATURE_FUNCTION_REGISTRY``.

        DAX calls are minimised:
          - 1 call for all cross-sectional features (in scope)
          - 1 call for all time-series features (extended date range)
          - Optional extra calls for PBI prior-period measures
        """
        filter_conditions = filter_conditions or {}
        self.last_kpi_dax_query = None
        self.feature_dax_queries = {}
        self.current_period_kpi = {}
        self.prior_period_kpi = {}
        self.last_kpi_format = "number"

        try:
            kpi_config = await self._loader.get_kpi_config(kpi_name)
        except Exception:
            logger.warning("Cannot load KPI config for '%s'. Feature generation skipped.", kpi_name)
            return {}

        self.last_kpi_format = kpi_config.kpi_format

        if is_self_dimension(dimension_name):
            return await self._generate_self_features_for_kpi(
                kpi_config=kpi_config,
                dimension_name=dimension_name,
                start_date=start_date,
                end_date=end_date,
                filter_conditions=filter_conditions,
                only_feature_names=only_feature_names,
            )

        dim_ref = await self._loader.resolve_dimension_ref(dimension_name)
        if not dim_ref:
            logger.warning("Dimension '%s' has no PBI mapping. Feature generation skipped.", dimension_name)
            return {}

        _pfx = _run_log_prefix(self._run_label)
        logger.info(
            "%sDAX feature pass | kpi=%s | dim=%s | period=%s→%s",
            _pfx,
            kpi_name,
            dimension_name,
            start_date,
            end_date,
        )

        mapped = _registry_keys_for_requested_features(only_feature_names)
        if only_feature_names is not None and mapped is not None and not mapped:
            logger.warning(
                "%sNo FEATURE_FUNCTION_REGISTRY entries match only_feature_names=%s — skipping.",
                _pfx,
                sorted({str(x) for x in only_feature_names}),
            )
            return {}

        feature_names = mapped or list(FEATURE_FUNCTION_REGISTRY.keys())
        cross_features, ts_features = split_by_category(feature_names)
        if mapped is not None:
            logger.debug(
                "%sFeature subset | requested=%d registry_keys=%d | cross=%d ts=%d",
                _pfx,
                len(set(only_feature_names or ())),
                len(feature_names),
                len(cross_features),
                len(ts_features),
            )

        results: dict[str, pd.DataFrame] = {}

        _ACCEL_NAMES = frozenset({"acceleration", "acceleration_abs"})
        accel_feats: list[str] = []
        simple_feats: list[str] = []
        complex_feats: list[str] = []
        if ts_features:
            accel_feats = [f for f in ts_features if _normalize_feature_name(f) in _ACCEL_NAMES]
            simple_feats = [f for f in ts_features if is_simple_dax_feature(f)]
            complex_feats = [
                f
                for f in ts_features
                if not is_simple_dax_feature(f)
                and _normalize_feature_name(f) not in _ACCEL_NAMES
            ]

        needs_current_period_fetch = bool(cross_features or simple_feats or accel_feats)

        # ── Phase A: Cross-sectional features + current-period KPI snapshot ──
        if needs_current_period_fetch:
            try:
                df_cross = await self._fetch_current_period(
                    kpi_config.pbi_measure_name, dim_ref,
                    start_date, end_date, kpi_config, filter_conditions,
                )
                # Build the current-period KPI lookup (dimension_value → KPI Value).
                # This reflects the exact same date window as dax_kpi_query.
                if not df_cross.empty and "dimension_value" in df_cross.columns and "KPI Value" in df_cross.columns:
                    for _, r in df_cross.iterrows():
                        dv = str(r["dimension_value"])
                        kv = r["KPI Value"]
                        if kv is not None and not pd.isna(kv):
                            self.current_period_kpi[dv] = _to_feature_float(kv)

                for feat in cross_features:
                    try:
                        fn = FEATURE_FUNCTION_REGISTRY.get(feat)
                        if fn and not df_cross.empty:
                            result = fn(df_cross, "KPI Value", [], **{})
                            if isinstance(result, pd.Series):
                                out = df_cross.copy()
                                out[feat] = result
                                results[feat] = out
                                self.feature_dax_queries[feat] = None
                            elif isinstance(result, pd.DataFrame):
                                results[feat] = result
                                self.feature_dax_queries[feat] = None
                    except Exception:
                        logger.exception(
                            "%sCross-sectional feature '%s' failed.", _pfx, feat
                        )
            except Exception:
                logger.exception("%sDAX fetch for cross-sectional features failed.", _pfx)

        # ── Phase B: Time-series features ────────────────────────────────────
        if ts_features:
            # B0: Acceleration — 2 exact prior-period queries, period inferred from
            # the signal job's date range so wow/mom/qoq/yoy all work correctly.
            #   current_growth = (cur  – prior_1) / |prior_1| × 100
            #   prev_growth    = (prior_1 – prior_2) / |prior_2| × 100
            #   acceleration   = current_growth – prev_growth
            for feat in accel_feats:
                try:
                    base_feat = _acceleration_base_feat(feat, start_date, end_date)
                    prior1_q = _build_prior_period_query(
                        base_feat, kpi_config.pbi_measure_name, dim_ref,
                        start_date, end_date,
                        self._settings, kpi_config, filter_conditions,
                    )
                    prior1_dict = await self._execute_prior_period_query(prior1_q, dim_ref)
                    if not prior1_dict:
                        logger.info(
                            "%sPrior period (t-1) has no data — skipping acceleration '%s'.",
                            _pfx,
                            feat,
                        )
                        continue

                    prior1_start, prior1_end = _prior_period_dates(base_feat, start_date, end_date)
                    prior2_q = _build_prior_period_query(
                        base_feat, kpi_config.pbi_measure_name, dim_ref,
                        prior1_start, prior1_end,
                        self._settings, kpi_config, filter_conditions,
                    )
                    prior2_dict = await self._execute_prior_period_query(prior2_q, dim_ref)
                    if not prior2_dict:
                        logger.info(
                            "%sPrior period (t-2) has no data — skipping acceleration '%s'.",
                            _pfx,
                            feat,
                        )
                        continue

                    df_feat = _compute_acceleration_exact(
                        feat,
                        self.current_period_kpi,
                        prior1_dict,
                        prior2_dict,
                        kpi_format=kpi_config.kpi_format,
                        kpi_name=kpi_config.kpi_name,
                    )
                    if not df_feat.empty:
                        results[feat] = df_feat
                        self.feature_dax_queries[feat] = prior1_q
                        logger.debug(
                            "%sFeature '%s' via 2-prior-period (base=%s).",
                            _pfx,
                            feat,
                            base_feat,
                        )
                except Exception:
                    logger.exception("%sAcceleration feature '%s' failed.", _pfx, feat)


            # B1: Simple features — execute current + prior DAX queries directly.
            # This gives correct period-specific values (actual prior week, not
            # monthly aggregates) and populates prior_period_kpi for each feature.
            for feat in simple_feats:
                try:
                    prior_q = _build_prior_period_query(
                        feat, kpi_config.pbi_measure_name, dim_ref,
                        start_date, end_date,
                        self._settings, kpi_config, filter_conditions,
                    )
                    self.feature_dax_queries[feat] = prior_q
                    prior_dict = await self._execute_prior_period_query(prior_q, dim_ref)
                    if not prior_dict:
                        logger.info(
                            "%sPrior period has no data — skipping feature '%s'.",
                            _pfx,
                            feat,
                        )
                        continue
                    self.prior_period_kpi[feat] = prior_dict
                    df_feat = _compute_feature_from_periods(
                        feat,
                        self.current_period_kpi,
                        prior_dict,
                        kpi_format=kpi_config.kpi_format,
                        kpi_name=kpi_config.kpi_name,
                    )
                    if not df_feat.empty:
                        results[feat] = df_feat
                        logger.debug(
                            "%sFeature '%s' via DAX prior-period (%s).",
                            _pfx,
                            feat,
                            prior_q.split("CALENDAR(")[1].split(")")[0]
                            if "CALENDAR(" in prior_q
                            else "?",
                        )
                    else:
                        logger.info(
                            "%sNo dimension rows with prior-period data — skipping feature '%s'.",
                            _pfx,
                            feat,
                        )
                except Exception:
                    logger.exception("%sDAX prior-period feature '%s' failed.", _pfx, feat)

            # B2: Complex features (acceleration, std_dev, etc.) — need full
            # multi-period monthly history; use the existing batch approach.
            if complex_feats:
                lookback = max_lookback_months(complex_feats)
                extended_start = start_date - relativedelta(months=lookback)
                try:
                    df_ts = await self._fetch_time_series(
                        kpi_config.pbi_measure_name, dim_ref,
                        extended_start, end_date, kpi_config, filter_conditions,
                    )
                    if not df_ts.empty:
                        for feat in complex_feats:
                            try:
                                fn = FEATURE_FUNCTION_REGISTRY.get(feat)
                                if not fn:
                                    continue
                                strat = _get_strategy(feat)
                                params = _default_params_for(feat, strat)
                                params["kpi_name"] = kpi_config.kpi_name

                                result = fn(df_ts, "KPI Value", ["_sort_key"], **params)
                                if isinstance(result, pd.Series):
                                    out = df_ts.copy()
                                    out[feat] = result
                                    current_month_start = pd.Timestamp(start_date.replace(day=1))
                                    current_mask = out["_period_start"] >= current_month_start
                                    filtered = out[current_mask].reset_index(drop=True)
                                    results[feat] = filtered
                                elif isinstance(result, pd.DataFrame):
                                    results[feat] = result

                                prior_q = _build_prior_period_query(
                                    feat, kpi_config.pbi_measure_name, dim_ref,
                                    start_date, end_date,
                                    self._settings, kpi_config, filter_conditions,
                                )
                                self.feature_dax_queries[feat] = prior_q
                            except Exception:
                                logger.exception("%sTime-series feature '%s' failed.", _pfx, feat)
                except Exception:
                    logger.exception(
                        "%sDAX fetch for complex time-series features failed.", _pfx
                    )

        return {
            name: _round_numeric_columns_in_feature_df(df)
            for name, df in results.items()
        }

    async def _generate_self_features_for_kpi(
        self,
        *,
        kpi_config: Any,
        dimension_name: str,
        start_date: date,
        end_date: date,
        filter_conditions: dict[str, list[str]],
        only_feature_names: Collection[str] | None,
    ) -> dict[str, pd.DataFrame]:
        """Portfolio-level raw KPI value for the job window (no dimension drill)."""
        _pfx = _run_log_prefix(self._run_label)
        logger.info(
            "%sDAX self feature pass | kpi=%s | dim=%s | period=%s→%s",
            _pfx,
            kpi_config.kpi_name,
            dimension_name or SELF_DIMENSION_NAME,
            start_date,
            end_date,
        )

        mapped = _registry_keys_for_requested_features(only_feature_names)
        if only_feature_names is not None and mapped is not None and not mapped:
            logger.warning(
                "%sNo FEATURE_FUNCTION_REGISTRY entries match only_feature_names=%s — skipping.",
                _pfx,
                sorted({str(x) for x in only_feature_names}),
            )
            return {}

        feature_names = mapped or list(FEATURE_FUNCTION_REGISTRY.keys())
        eligible = [f for f in feature_names if is_self_feature(f)]
        if not eligible:
            logger.warning(
                "%sSELF dimension supports self_kpi_value only. Requested: %s",
                _pfx,
                feature_names,
            )
            return {}

        results: dict[str, pd.DataFrame] = {}
        measure = kpi_config.pbi_measure_name

        try:
            cur_val, cur_q = await self._fetch_scalar_kpi(
                measure, start_date, end_date, kpi_config, filter_conditions,
            )
            self.last_kpi_dax_query = cur_q
            if cur_val is None:
                logger.warning("%sSELF KPI fetch returned no value.", _pfx)
                return {}
            self.current_period_kpi = {SELF_DIMENSION_VALUE: cur_val}
        except Exception:
            logger.exception("%sDAX fetch for self KPI value failed.", _pfx)
            return {}

        base_row = {
            "dimension_value": SELF_DIMENSION_VALUE,
            "KPI Value": cur_val,
        }

        for feat in eligible:
            try:
                fn = FEATURE_FUNCTION_REGISTRY.get(feat)
                if not fn:
                    continue
                df = pd.DataFrame([base_row])
                result = fn(df, "KPI Value", [], **{})
                feat_col = _normalize_feature_name(feat)
                if isinstance(result, pd.Series):
                    df[feat_col] = result
                elif isinstance(result, pd.DataFrame):
                    df = result
                else:
                    df[feat_col] = cur_val
                results[feat] = df
                self.feature_dax_queries[feat] = None
                logger.debug("%sSELF feature '%s' | kpi_value=%s", _pfx, feat_col, cur_val)
            except Exception:
                logger.exception("%sSELF feature '%s' failed.", _pfx, feat)

        return {
            name: _round_numeric_columns_in_feature_df(df)
            for name, df in results.items()
        }

    # ── DAX fetch helpers ────────────────────────────────────────────────────

    async def _fetch_current_period(
        self,
        measure: str,
        dim_ref: DimensionRef,
        start_date: date,
        end_date: date,
        kpi_config: Any,
        filters: dict[str, list[str]],
    ) -> pd.DataFrame:
        builder = (
            DAXQueryBuilder()
            .with_kpi(measure)
            .group_by(dim_ref)
            .add_date_filter(self._settings.DATE_TABLE_NAME, self._settings.DATE_COLUMN_NAME, start_date, end_date)
        )
        builder = _apply_filters(builder, kpi_config, filters)
        query = builder.build()
        self.last_kpi_dax_query = query
        rows = await self._pbi.execute_dax(query)
        return _to_dataframe(rows, dim_ref)

    async def _fetch_scalar_kpi(
        self,
        measure: str,
        start_date: date,
        end_date: date,
        kpi_config: Any,
        filters: dict[str, list[str]],
    ) -> tuple[float | None, str]:
        """Return portfolio KPI total for a date window (no SUMMARIZECOLUMNS group-by)."""
        query = _build_scalar_period_query(
            measure,
            start_date,
            end_date,
            self._settings,
            kpi_config,
            filters,
        )
        val = await self._fetch_scalar_kpi_from_query(query)
        return val, query

    async def _fetch_scalar_kpi_from_query(self, query: str) -> float | None:
        rows = await self._pbi.execute_dax(query)
        return _scalar_kpi_from_rows(rows)

    async def _fetch_time_series(
        self,
        measure: str,
        dim_ref: DimensionRef,
        start_date: date,
        end_date: date,
        kpi_config: Any,
        filters: dict[str, list[str]],
    ) -> pd.DataFrame:
        """Fetch multi-period data grouped by dimension + month."""
        month_dim = DimensionRef(
            dimension_name="_month",
            pbi_table_name=self._settings.DATE_TABLE_NAME,
            pbi_column_name="MonthName",
        )
        year_dim = DimensionRef(
            dimension_name="_year",
            pbi_table_name=self._settings.DATE_TABLE_NAME,
            pbi_column_name="Year",
        )
        builder = (
            DAXQueryBuilder()
            .with_kpi(measure)
            .group_by(dim_ref, year_dim, month_dim)
            .add_date_filter(self._settings.DATE_TABLE_NAME, self._settings.DATE_COLUMN_NAME, start_date, end_date)
        )
        builder = _apply_filters(builder, kpi_config, filters)
        query = builder.build()
        rows = await self._pbi.execute_dax(query)
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df = _normalize_columns(df, dim_ref)

        year_col = _find_col(df, "Year")
        month_col = _find_col(df, "MonthName")
        if year_col and month_col:
            df["year"] = pd.to_numeric(df[year_col], errors="coerce")
            df["month"] = df[month_col].apply(_month_name_to_num)
            df["_sort_key"] = df["year"] * 100 + df["month"]
            df["_period_start"] = pd.to_datetime(
                df["year"].astype(int).astype(str) + "-" + df["month"].astype(int).astype(str) + "-01",
                errors="coerce",
            )
            df.sort_values(["_sort_key", "dimension_value"], inplace=True)
            df.reset_index(drop=True, inplace=True)

        return df

    async def _execute_prior_period_query(
        self,
        query: str,
        dim_ref: DimensionRef,
    ) -> dict[str, float]:
        """Execute a pre-built prior-period DAX query.

        Returns ``{dimension_value: KPI Value}`` — one entry per dimension.
        For rolling-window queries this is the aggregate over the whole window
        (used as the rolling baseline), not a monthly series.
        """
        rows = await self._pbi.execute_dax(query)
        df = _to_dataframe(rows, dim_ref)
        if df.empty or "KPI Value" not in df.columns:
            return {}
        result: dict[str, float] = {}
        for _, row in df.iterrows():
            dv = str(row["dimension_value"])
            kv = row["KPI Value"]
            if kv is not None and not pd.isna(kv):
                result[dv] = _to_feature_float(kv)
        return result


# ── Helpers ──────────────────────────────────────────────────────────────────

def _apply_filters(
    builder: DAXQueryBuilder,
    kpi_config: Any,
    filters: dict[str, list[str]],
) -> DAXQueryBuilder:
    valid_names = {d.dimension_name for d in kpi_config.valid_dimensions}
    for dim_name, vals in filters.items():
        fc_dim = resolve_job_filter_dimension(kpi_config, dim_name)
        if fc_dim:
            builder = builder.add_member_filter(fc_dim, vals)
        else:
            logger.warning(
                "%sJob filter ignored: dimension %r is not a valid dimension for KPI %r "
                "(valid: %s). Add it to config_kpi_valid_dimensions + config_dimensionsreunityportal, "
                "or fix the filter key spelling.",
                _run_log_prefix(""),
                dim_name,
                kpi_config.kpi_name,
                sorted(valid_names),
            )
    return builder


def _to_dataframe(rows: list[dict[str, Any]], dim_ref: DimensionRef) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = _normalize_columns(df, dim_ref)
    return df


def _normalize_columns(df: pd.DataFrame, dim_ref: DimensionRef) -> pd.DataFrame:
    """Rename PBI response columns to standard names."""
    rename_map: dict[str, str] = {}
    for col in df.columns:
        lower = col.lower()
        if dim_ref.pbi_column_name.lower() in lower:
            rename_map[col] = "dimension_value"
        if "kpi value" in lower:
            rename_map[col] = "KPI Value"
    df = df.rename(columns=rename_map)
    if "KPI Value" in df.columns:
        df["KPI Value"] = pd.to_numeric(df["KPI Value"], errors="coerce")
    return df


def _find_col(df: pd.DataFrame, fragment: str) -> str | None:
    frag_lower = fragment.lower()
    for c in df.columns:
        if frag_lower in c.lower():
            return c
    return None


_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _month_name_to_num(name: Any) -> int:
    if name is None:
        return 0
    s = str(name).strip().lower()
    return _MONTH_MAP.get(s, 0)


def _compute_feature_from_periods(
    feat: str,
    current_kpi: dict[str, float],
    prior_kpi: dict[str, float],
    kpi_format: str = "number",
    kpi_name: str = "",
) -> pd.DataFrame:
    """Compute a simple feature value per dimension from two period KPI values.

    For KPIs in ``set_rate``, ``issue_rate``, ``demo_rate``, ``gross_close_rate``,
    ``net_close_rate`` (by ``kpi_name``): change = **percentage points**
    ``scale(current) − scale(prior)`` where ``scale`` maps fractional rates (e.g. 0.21)
    to a 0–100 percent scale.

    For other ``kpi_format=percentage`` KPIs: relative ``(a−b)/|b|×100`` on percent scale.

    For non-percentage KPIs (and non-rate names above): ``(current - prior) / |prior| * 100``.

    The returned DataFrame has columns: ``dimension_value``, ``KPI Value``,
    ``Prior Value``, and the feature column named ``feat``.
    """
    name = _normalize_feature_name(feat)
    is_pct_kpi = kpi_format.lower().strip() == "percentage"
    use_pp = bool(kpi_name) and kpi_uses_percentage_point_difference(kpi_name)
    rows = []
    for dim_val, cur in current_kpi.items():
        prev = prior_kpi.get(dim_val)
        if prev is None:
            continue
        feat_val: float | None = None
        if "growth_abs" in name:
            if use_pp:
                feat_val = kpi_storage_to_percent_point_scale(
                    cur
                ) - kpi_storage_to_percent_point_scale(prev)
            else:
                feat_val = cur - prev
        elif use_pp:
            cur_p = kpi_storage_to_percent_point_scale(cur)
            prev_p = kpi_storage_to_percent_point_scale(prev)
            feat_val = cur_p - prev_p
        elif is_pct_kpi:
            cur_p = kpi_storage_to_percent_point_scale(cur)
            prev_p = kpi_storage_to_percent_point_scale(prev)
            if prev_p != 0:
                feat_val = ((cur_p - prev_p) / abs(prev_p)) * 100
        elif prev != 0:
            feat_val = ((cur - prev) / abs(prev)) * 100
        if feat_val is None:
            continue
        rows.append({
            "dimension_value": dim_val,
            "KPI Value": _to_feature_float(cur),
            "Prior Value": _to_feature_float_optional(prev),
            feat: _to_feature_float_optional(feat_val),
        })
    return pd.DataFrame(rows)


def _acceleration_base_feat(feat: str, start_date: date, end_date: date) -> str:
    """Return the growth feature whose prior-period window matches the signal date range.

    Uses the date window length to infer the granularity, so acceleration
    automatically aligns with wow/mom/qoq/yoy without any extra config.
    """
    period_days = (end_date - start_date).days + 1
    if period_days <= 14:
        return "wow_growth_pct"
    if period_days <= 95:
        return "mom_growth_pct"
    if period_days <= 100:
        return "qoq_growth_pct"
    return "yoy_growth_pct"



def _compute_acceleration_exact(
    feat: str,
    current_kpi: dict[str, float],
    prior_kpi: dict[str, float],
    prior_prior_kpi: dict[str, float],
    kpi_format: str = "number",
    kpi_name: str = "",
) -> pd.DataFrame:
    """Compute acceleration from three period-exact KPI snapshots.

    For **rate** KPIs (``set_rate``, ``issue_rate``, … — see ``kpi_uses_percentage_point_difference``),
    growth legs are **percentage-point** moves; acceleration is the delta of those legs.
    Other percentage KPIs use relative ``(a−b)/|b|×100`` per leg.

    For acceleration_abs on PP-rate KPIs, second-differences use the same % scale.

    For non-percentage KPIs, uses raw ratio legs as before.
    """
    name = _normalize_feature_name(feat)
    is_abs = "abs" in name
    is_pct_kpi = kpi_format.lower().strip() == "percentage"
    use_pp = bool(kpi_name) and kpi_uses_percentage_point_difference(kpi_name)
    rows = []
    for dim_val, cur in current_kpi.items():
        p1 = prior_kpi.get(dim_val)
        p2 = prior_prior_kpi.get(dim_val)
        feat_val: float | None = None
        if p1 is not None and p2 is not None:
            if is_abs:
                if use_pp:
                    c = kpi_storage_to_percent_point_scale(cur)
                    a = kpi_storage_to_percent_point_scale(p1)
                    b = kpi_storage_to_percent_point_scale(p2)
                    feat_val = (c - a) - (a - b)
                else:
                    feat_val = (cur - p1) - (p1 - p2)
            elif use_pp:
                c = kpi_storage_to_percent_point_scale(cur)
                a = kpi_storage_to_percent_point_scale(p1)
                b = kpi_storage_to_percent_point_scale(p2)
                current_growth = c - a
                prev_growth = a - b
                feat_val = current_growth - prev_growth
            elif p1 != 0 and p2 != 0:
                if is_pct_kpi:
                    c = kpi_storage_to_percent_point_scale(cur)
                    a = kpi_storage_to_percent_point_scale(p1)
                    b = kpi_storage_to_percent_point_scale(p2)
                    current_growth = ((c - a) / abs(a)) * 100
                    prev_growth = ((a - b) / abs(b)) * 100
                else:
                    current_growth = ((cur - p1) / abs(p1)) * 100
                    prev_growth = ((p1 - p2) / abs(p2)) * 100
                feat_val = current_growth - prev_growth
        if feat_val is None:
            continue
        rows.append({
            "dimension_value": dim_val,
            "KPI Value": _to_feature_float(cur),
            "Prior Value": _to_feature_float_optional(p1),
            feat: _to_feature_float_optional(feat_val),
        })
    return pd.DataFrame(rows)


def _prior_period_dates(
    feat: str, start_date: date, end_date: date,
) -> tuple[date, date]:
    """Return (prior_start, prior_end) for the feature's comparison period.

    The returned range is what you'd query in Power BI to get the "other"
    value needed alongside the current KPI value to compute the feature.
    """
    name = _normalize_feature_name(feat)
    period_days = (end_date - start_date).days + 1

    if "wow" in name:
        return (start_date - timedelta(days=7),
                start_date - timedelta(days=1))

    if "wom" in name:
        return (start_date - timedelta(days=7),
                start_date - timedelta(days=1))

    if "mom" in name:
        ps = start_date - relativedelta(months=1)
        pe = end_date - relativedelta(months=1)
        return (ps, pe)

    if "yoy" in name:
        ps = start_date - relativedelta(years=1)
        pe = end_date - relativedelta(years=1)
        return (ps, pe)

    if "qoq" in name:
        ps = start_date - relativedelta(months=3)
        pe = end_date - relativedelta(months=3)
        return (ps, pe)

    if "acceleration" in name:
        shift = timedelta(days=7) if period_days <= 14 else relativedelta(months=1)
        return (start_date - shift, start_date - timedelta(days=1))

    if "kpi_vs_rolling" in name:
        window = 3
        return (start_date - relativedelta(months=window),
                start_date - timedelta(days=1))

    if "rolling_avg" in name:
        window = 6 if "6m" in name else (12 if "12m" in name else 3)
        return (start_date - relativedelta(months=window),
                start_date - timedelta(days=1))

    if "std_dev" in name:
        window = 3 if "3m" in name else 6
        return (start_date - relativedelta(months=window),
                start_date - timedelta(days=1))

    if "growth" in name:
        if period_days <= 14:
            return (start_date - timedelta(days=period_days),
                    start_date - timedelta(days=1))
        return (start_date - relativedelta(months=1),
                end_date - relativedelta(months=1))

    return (start_date - timedelta(days=period_days),
            start_date - timedelta(days=1))


def _build_prior_period_query(
    feat: str,
    measure: str,
    dim_ref: DimensionRef,
    start_date: date,
    end_date: date,
    settings: Any,
    kpi_config: Any,
    filters: dict[str, list[str]],
) -> str:
    """Build default kwargs for the Python feature function.

    All time-series features that compute per-entity (growth, acceleration,
    rolling averages, std_dev) receive ``groupby_columns=["dimension_value"]``
    so that pct_change / diff / rolling operations are scoped per dimension
    member, not across the entire flat sorted DataFrame.
    """
    prior_start, prior_end = _prior_period_dates(feat, start_date, end_date)
    builder = (
        DAXQueryBuilder()
        .with_kpi(measure)
        .group_by(dim_ref)
        .add_date_filter(settings.DATE_TABLE_NAME, settings.DATE_COLUMN_NAME,
                         prior_start, prior_end)
    )
    builder = _apply_filters(builder, kpi_config, filters)
    return builder.build()


def _build_scalar_period_query(
    measure: str,
    start_date: date,
    end_date: date,
    settings: Any,
    kpi_config: Any,
    filters: dict[str, list[str]],
) -> str:
    """DAX for a single portfolio KPI total (no dimension group-by)."""
    builder = (
        DAXQueryBuilder()
        .with_kpi(measure)
        .add_date_filter(
            settings.DATE_TABLE_NAME,
            settings.DATE_COLUMN_NAME,
            start_date,
            end_date,
        )
    )
    builder = _apply_filters(builder, kpi_config, filters)
    return builder.build()


def _scalar_kpi_from_rows(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    row = rows[0]
    for key, val in row.items():
        if "kpi value" in str(key).lower():
            return _to_feature_float_optional(val)
    for val in row.values():
        parsed = _to_feature_float_optional(val)
        if parsed is not None:
            return parsed
    return None


def _default_params_for(feat: str, strat: FeatureDAXStrategy) -> dict[str, Any]:
    """Build default kwargs for the Python feature function."""
    params: dict[str, Any] = {}
    if "yoy" in feat:
        params["periods"] = 12
        params["strict_calendar_year_growth"] = True
        params["groupby_columns"] = ["dimension_value"]
    elif "qoq" in feat:
        params["periods"] = 3
        params["strict_calendar_quarter_growth"] = True
        params["groupby_columns"] = ["dimension_value"]
    elif "wow" in feat:
        params["periods"] = 1
    elif "6m" in feat:
        params["window"] = 6
    elif "12m" in feat:
        params["window"] = 12
    elif "3m" in feat:
        params["window"] = 3
    elif "rolling_avg" in feat:
        params["window"] = 3
    elif "std_dev" in feat:
        params["window"] = 6
    elif "mom" in feat or "growth" in feat:
        params["periods"] = 1
    elif "kpi_vs_rolling" in feat:
        params["window"] = 3
    return params
