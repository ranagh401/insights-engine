"""DAX-native feature computation strategies.

Categorises features into:
  * CROSS_SECTIONAL  – only need current-period data grouped by dimension
  * TIME_SERIES      – need multi-period history; extend date range, group by
                       time grain, then apply Python logic

Portal fork: prior-period values come from date-shifted DAX queries (Phase B),
not from pre-built PBI measure names like ``[KPI Prior 4W]``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureDAXStrategy:
    """Describes how to compute a feature using DAX."""
    category: str                       # "cross_sectional" or "time_series"
    lookback_months: int = 0            # how far back to extend the date range
    time_grain: str = "month"           # "month" or "week"

FEATURE_STRATEGIES: dict[str, FeatureDAXStrategy] = {
    # ── Cross-sectional (single-period fetch) ────────────────────────────────
    "rank":               FeatureDAXStrategy("cross_sectional"),
    "rank_pct":           FeatureDAXStrategy("cross_sectional"),
    "sku_mix":            FeatureDAXStrategy("cross_sectional"),
    "channel_mix":        FeatureDAXStrategy("cross_sectional"),
    "region_mix":         FeatureDAXStrategy("cross_sectional"),
    "benchmark_variance": FeatureDAXStrategy("cross_sectional"),
    "z_score":            FeatureDAXStrategy("cross_sectional"),

    # ── Time-series (multi-period history) ───────────────────────────────────
    "growth_pct":         FeatureDAXStrategy("time_series", lookback_months=2, time_grain="month"),
    "mom_growth_pct":     FeatureDAXStrategy("time_series", lookback_months=2, time_grain="month"),
    "wow_growth_pct":     FeatureDAXStrategy("time_series", lookback_months=1, time_grain="week"),
    "self_kpi_value":     FeatureDAXStrategy("cross_sectional"),
    "yoy_growth_pct":     FeatureDAXStrategy("time_series", lookback_months=13, time_grain="month"),    "qoq_growth_pct":     FeatureDAXStrategy("time_series", lookback_months=4, time_grain="month"),
    "growth_abs":         FeatureDAXStrategy("time_series", lookback_months=2, time_grain="month"),
    "acceleration":       FeatureDAXStrategy("time_series", lookback_months=3, time_grain="month"),
    "acceleration_abs":   FeatureDAXStrategy("time_series", lookback_months=3, time_grain="month"),
    "rolling_avg":        FeatureDAXStrategy("time_series", lookback_months=4, time_grain="month"),
    "rolling_avg_6m":     FeatureDAXStrategy("time_series", lookback_months=7, time_grain="month"),
    "rolling_avg_12m":    FeatureDAXStrategy("time_series", lookback_months=13, time_grain="month"),
    "kpi_vs_rolling_avg_pct": FeatureDAXStrategy("time_series", lookback_months=4, time_grain="month"),    "std_dev":            FeatureDAXStrategy("time_series", lookback_months=7, time_grain="month"),
    "std_dev_3m":         FeatureDAXStrategy("time_series", lookback_months=4, time_grain="month"),
}


# Features that need exactly ONE prior-period DAX query and whose value can be
# computed directly from (current_kpi, prior_kpi) without a monthly time-series.
# These get accurate period-specific values (e.g. actual prior week, not monthly avg).
SIMPLE_DAX_FEATURES = frozenset({
    "wow_growth_pct",
    "wom_growth_pct",
    "mom_growth_pct",
    "yoy_growth_pct",
    "qoq_growth_pct",
    "growth_pct",
    "growth_abs",
    "kpi_vs_rolling_avg_pct",
})


def is_simple_dax_feature(feat_name: str) -> bool:
    """True when the feature can be computed from two DAX calls (current + prior)."""
    return _normalize_feature_name(feat_name) in SIMPLE_DAX_FEATURES


def _normalize_feature_name(name: str) -> str:
    """Strip ``calculate_`` prefix so registry keys match strategy keys."""
    return name[len("calculate_"):] if name.startswith("calculate_") else name


def _get_strategy(feature_name: str) -> FeatureDAXStrategy:
    """Look up strategy, handling the ``calculate_`` prefix transparently."""
    key = _normalize_feature_name(feature_name)
    return FEATURE_STRATEGIES.get(key, FeatureDAXStrategy("cross_sectional"))


def max_lookback_months(feature_names: list[str]) -> int:
    """Return the largest lookback needed across all requested features."""
    return max(
        (_get_strategy(f).lookback_months for f in feature_names),
        default=0,
    )


def split_by_category(
    feature_names: list[str],
) -> tuple[list[str], list[str]]:
    """Split into (cross_sectional, time_series) lists."""
    cross, ts = [], []
    for f in feature_names:
        strat = _get_strategy(f)
        if strat.category == "time_series":
            ts.append(f)
        else:
            cross.append(f)
    return cross, ts
